"""Parallelizability Scheduler for SquidAgent.

Analyzes a task DAG and produces an execution wave schedule that controls
whether each dependency layer runs SERIAL or STAGED_PARALLEL.

The scheduler runs OUTSIDE the orchestrator LLM — it is pure Python code
called via an MCP tool after the orchestrator creates a plan but before
any workers are launched.

Decision mechanism: per-layer token-cost inequality.

For each dependency layer L_k of the task DAG:
    T_serial(L_k)   = sum(estimated_output_tokens_i)          — serial cost
    T_parallel(L_k) = max(estimated_output_tokens_i) + C_align(L_k)
                                                               — parallel cost

    If T_serial / T_parallel > PARALLEL_MARGIN  → STAGED_PARALLEL
    Otherwise                                   → SERIAL

C_align(L_k) is the layer's alignment cost (the shared conventions written
before launching the layer's workers), estimated by the orchestrator during
planning. The margin absorbs estimation noise.

Optional per-layer bypass (agent-tool mode): if a layer's total estimated
tokens is below bypass_threshold, the whole layer is written directly by the
main agent (SERIAL), without spawning subagents.
"""

from __future__ import annotations

import re
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Strategy(Enum):
    SERIAL = "serial"
    STAGED_PARALLEL = "staged_parallel"  # parallel, preceded by a shared conventions block


@dataclass
class TaskNode:
    id: str
    description: str
    depends_on: List[str] = field(default_factory=list)
    enables: List[str] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    preconditions: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    estimated_time_s: float = 480.0  # default 8 min
    priority: str = "medium"
    estimated_output_tokens: int = 0
    strategy: Strategy = Strategy.SERIAL  # assigned by decide_strategies()


@dataclass
class GroupDecision:
    """Records the inequality-based decision for one group of tasks."""
    group_id: str
    task_ids: List[str]
    T_serial_tokens: int      # sum of estimated_output_tokens
    T_parallel_tokens: int    # plan_cost + max(estimated_output_tokens)
    plan_cost_tokens: int     # per-layer alignment cost
    max_task_tokens: int      # max single task tokens
    ratio: float              # T_serial / T_parallel
    strategy: Strategy
    reasoning: str            # human-readable decision explanation

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "task_ids": self.task_ids,
            "T_serial_tokens": self.T_serial_tokens,
            "T_parallel_tokens": self.T_parallel_tokens,
            "plan_cost_tokens": self.plan_cost_tokens,
            "max_task_tokens": self.max_task_tokens,
            "ratio": self.ratio,
            "strategy": self.strategy.value,
            "reasoning": self.reasoning,
        }


@dataclass
class ExecutionWave:
    wave_index: int
    task_ids: List[str]
    strategy: Strategy
    conventions_prompt: Optional[str] = None  # set for parallel waves: orch must output conventions first

    def to_dict(self) -> dict:
        d = {
            "wave_index": self.wave_index,
            "task_ids": self.task_ids,
            "strategy": self.strategy.value,
        }
        if self.conventions_prompt:
            d["conventions_prompt"] = self.conventions_prompt
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_time_to_seconds(time_str: str) -> float:
    """Parse estimated time strings like '8 minutes', '1.5 hours', '30s'."""
    if not time_str:
        return 0.0  # no estimate provided — let caller use next fallback
    s = time_str.strip().lower()
    m = re.match(r'([\d.]+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?)', s)
    if not m:
        # Try bare number — assume minutes
        try:
            return float(s) * 60
        except ValueError:
            return 480.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit.startswith('s'):
        return val
    elif unit.startswith('m'):
        return val * 60
    else:  # hours
        return val * 3600


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _tokenize(text: str) -> List[str]:
    """Split description into lowercase word tokens, filtering stopwords."""
    _STOP = {
        'a', 'an', 'the', 'and', 'or', 'of', 'to', 'in', 'for', 'on',
        'with', 'at', 'by', 'from', 'is', 'it', 'be', 'as', 'that',
        'this', 'are', 'was', 'will', 'must', 'should', 'can', 'do',
        'each', 'all', 'any', 'its', 'not', 'but', 'if', 'no', 'so',
    }
    words = re.findall(r'[a-z0-9_]+', text.lower())
    return [w for w in words if w not in _STOP and len(w) > 1]


# ---------------------------------------------------------------------------
# Main scheduler
# ---------------------------------------------------------------------------

class ParallelizabilityScheduler:
    """Decides parallel vs serial execution per dependency layer.

    Each layer is treated as a single group (workers share the orchestrator's
    context via session fork, so no consensus-group splitting is needed).
    Tasks below SERIAL_THRESHOLD_TOKENS are split out and always run serially.
    """

    PARALLEL_MARGIN = 1.9  # safety margin alpha
    TOKENS_PER_SECOND = 17
    CONVENTION_PLANNING_TOKENS_DEFAULT = 3000
    SERIAL_THRESHOLD_TOKENS = 1000  # tasks below this are always serial (worker overhead > benefit)

    def __init__(self, max_parallel: int = 6, domain_context: str = "",
                 bypass_threshold: int = 0, agent_tool: bool = False):
        """
        Args:
            max_parallel: Maximum concurrent workers.
            domain_context: User-injected background knowledge that helps
                judge consensus risk.
            bypass_threshold: Per-layer bypass (0 = disabled). Layers whose
                total estimated tokens is below this run SERIAL, no subagents.
            agent_tool: Word the conventions guidance for agent-tool mode
                (conventions reach subagents through their Agent prompts)
                instead of worktree mode (workers fork the orchestrator session).
        """
        self.max_parallel = max_parallel
        self.domain_context = domain_context
        self.bypass_threshold = bypass_threshold
        self.agent_tool = agent_tool
        # Cached between decide_strategies and generate_waves
        self._cached_layer_groups: Dict[str, List[List[str]]] = {}
        # Risk factors generated during strategy decision
        self._risk_factors: List[str] = []

    # ---- DAG construction ----

    def build_dag(self, tasks: List[Dict]) -> Dict[str, TaskNode]:
        """Parse plan task list into a DAG of TaskNode objects."""
        dag: Dict[str, TaskNode] = {}
        for t in tasks:
            tid = t.get("id", "")
            if not tid:
                continue
            # Parse depends_on from related_tasks or top-level
            related = t.get("related_tasks", {})
            depends = related.get("depends_on", []) if isinstance(related, dict) else []
            enables = related.get("enables", []) if isinstance(related, dict) else []
            # Also check top-level depends_on (some plan formats)
            if not depends:
                depends = t.get("depends_on", [])
            if not enables:
                enables = t.get("enables", [])

            # Token estimation with three-tier fallback
            estimated_output_tokens = int(t.get("estimated_output_tokens", 0) or 0)
            if not estimated_output_tokens:
                time_s = _parse_time_to_seconds(t.get("estimated_time", ""))
                estimated_output_tokens = int(time_s * self.TOKENS_PER_SECOND)
            if not estimated_output_tokens:
                # Fallback: estimate from description word count (rough proxy)
                desc_words = len(t.get("description", "").split())
                estimated_output_tokens = max(desc_words * 30, 500)

            dag[tid] = TaskNode(
                id=tid,
                description=t.get("description", ""),
                depends_on=depends or [],
                enables=enables or [],
                provides=t.get("provides", []) or [],
                preconditions=t.get("preconditions", []) or [],
                files=t.get("files", []) or [],
                estimated_time_s=_parse_time_to_seconds(
                    t.get("estimated_time", "")
                ),
                priority=t.get("priority", "medium"),
                estimated_output_tokens=estimated_output_tokens,
            )

        # Auto-derive enables from depends_on (reverse edges)
        for node in dag.values():
            for dep_id in node.depends_on:
                if dep_id in dag and node.id not in dag[dep_id].enables:
                    dag[dep_id].enables.append(node.id)

        return dag

    # ---- Strategy decision ----

    def decide_strategies(
        self,
        dag: Dict[str, TaskNode],
        convention_planning_tokens: int = 0,
        layer_convention_tokens: Dict[str, int] = None,
    ) -> List[GroupDecision]:
        """Token-cost inequality per layer.

        Small tasks (below SERIAL_THRESHOLD_TOKENS) are split out and marked serial.

        If layer_convention_tokens is provided (e.g., {"L0": 3000, "L1": 5000}),
        each layer uses its own C_align. Otherwise falls back to the single
        convention_planning_tokens value for all layers.
        """
        default_plan_cost = convention_planning_tokens or self.CONVENTION_PLANNING_TOKENS_DEFAULT
        _layer_tokens = layer_convention_tokens or {}
        layers = self._topological_layers(dag)
        self._cached_layer_groups = {}
        decisions: List[GroupDecision] = []
        group_idx = 0

        for layer_idx, layer in enumerate(layers):
            layer_key = ','.join(sorted(layer))
            # Per-layer C_align: use layer_convention_tokens if available, else fallback
            plan_cost = _layer_tokens.get(f"L{layer_idx}", default_plan_cost)

            # Bypass: entire layer below threshold → written directly, no subagents
            if self.bypass_threshold > 0:
                layer_total = sum(dag[tid].estimated_output_tokens for tid in layer)
                if layer_total < self.bypass_threshold:
                    for tid in layer:
                        dag[tid].strategy = Strategy.SERIAL
                    decisions.append(GroupDecision(
                        group_id=f"g{group_idx}",
                        task_ids=list(layer),
                        T_serial_tokens=layer_total,
                        T_parallel_tokens=layer_total,
                        plan_cost_tokens=0,
                        max_task_tokens=max(dag[tid].estimated_output_tokens for tid in layer),
                        ratio=1.0,
                        strategy=Strategy.SERIAL,
                        reasoning=f"BYPASS: layer total ({layer_total}) < threshold ({self.bypass_threshold})",
                    ))
                    self._cached_layer_groups[layer_key] = [list(layer)]
                    group_idx += 1
                    continue

            # Split: separate big tasks (parallel candidates) from small tasks (always serial)
            big = [tid for tid in layer if dag[tid].estimated_output_tokens >= self.SERIAL_THRESHOLD_TOKENS]
            small = [tid for tid in layer if dag[tid].estimated_output_tokens < self.SERIAL_THRESHOLD_TOKENS]

            groups = []

            # Small tasks → serial group
            if small:
                for tid in small:
                    dag[tid].strategy = Strategy.SERIAL
                small_total = sum(dag[tid].estimated_output_tokens for tid in small)
                decisions.append(GroupDecision(
                    group_id=f"g{group_idx}",
                    task_ids=small,
                    T_serial_tokens=small_total,
                    T_parallel_tokens=small_total,
                    plan_cost_tokens=0,
                    max_task_tokens=max(dag[tid].estimated_output_tokens for tid in small),
                    ratio=1.0,
                    strategy=Strategy.SERIAL,
                    reasoning=f"All tasks below serial threshold ({self.SERIAL_THRESHOLD_TOKENS})",
                ))
                groups.append(small)
                group_idx += 1

            # Big tasks → single group, apply token inequality
            if not big:
                self._cached_layer_groups[layer_key] = groups
                continue

            if len(big) == 1:
                dag[big[0]].strategy = Strategy.SERIAL
                tok = dag[big[0]].estimated_output_tokens
                decisions.append(GroupDecision(
                    group_id=f"g{group_idx}",
                    task_ids=big,
                    T_serial_tokens=tok,
                    T_parallel_tokens=tok,
                    plan_cost_tokens=0,
                    max_task_tokens=tok,
                    ratio=1.0,
                    strategy=Strategy.SERIAL,
                    reasoning="Single task — write directly",
                ))
                groups.append(big)
                group_idx += 1
                self._cached_layer_groups[layer_key] = groups
                continue

            tokens = [dag[tid].estimated_output_tokens for tid in big]
            T_serial = sum(tokens)
            max_t = max(tokens)
            T_parallel = plan_cost + max_t
            ratio = T_serial / T_parallel if T_parallel > 0 else float('inf')
            ratio = round(ratio, 3)

            if ratio > self.PARALLEL_MARGIN:
                strategy = Strategy.STAGED_PARALLEL
                reasoning = (
                    f"T_serial ({T_serial}) / T_parallel ({T_parallel}) "
                    f"= {ratio}x > margin {self.PARALLEL_MARGIN}x → PARALLEL"
                )
            else:
                strategy = Strategy.SERIAL
                reasoning = (
                    f"T_serial ({T_serial}) / T_parallel ({T_parallel}) "
                    f"= {ratio}x <= margin {self.PARALLEL_MARGIN}x → SERIAL"
                )

            for tid in big:
                dag[tid].strategy = strategy

            decisions.append(GroupDecision(
                group_id=f"g{group_idx}",
                task_ids=big,
                T_serial_tokens=T_serial,
                T_parallel_tokens=T_parallel,
                plan_cost_tokens=plan_cost,
                max_task_tokens=max_t,
                ratio=ratio,
                strategy=strategy,
                reasoning=reasoning,
            ))
            groups.append(big)
            group_idx += 1
            self._cached_layer_groups[layer_key] = groups

        return decisions

    # ---- Wave generation ----

    def generate_waves(self, dag: Dict[str, TaskNode]) -> List[ExecutionWave]:
        """Topological layers → per-layer waves.

        Within a layer, all STAGED_PARALLEL tasks are merged into a single wave
        (with a combined conventions_prompt). SERIAL tasks produce one wave
        per task.

        Uses cached groups from decide_strategies() to avoid recomputation."""
        layers = self._topological_layers(dag)
        waves: List[ExecutionWave] = []
        wave_idx = 0

        for layer in layers:
            layer_key = ','.join(sorted(layer))
            groups = self._cached_layer_groups.get(layer_key)
            if groups is None:
                groups = [list(layer)]

            # Collect same-layer parallel vs serial tasks
            parallel_tasks: List[str] = []
            serial_tasks: List[str] = []
            conventions_prompts: List[str] = []

            for group in groups:
                if dag[group[0]].strategy == Strategy.STAGED_PARALLEL:
                    parallel_tasks.extend(group)
                    conventions_prompts.append(
                        self._build_conventions_prompt(group, dag)
                    )
                else:
                    serial_tasks.extend(group)

            # Merged parallel wave (all STAGED_PARALLEL tasks from this layer)
            if parallel_tasks:
                waves.append(ExecutionWave(
                    wave_index=wave_idx,
                    task_ids=parallel_tasks,
                    strategy=Strategy.STAGED_PARALLEL,
                    conventions_prompt='\n\n---\n\n'.join(conventions_prompts),
                ))
                wave_idx += 1

            # Serial tasks: one wave per task
            for tid in serial_tasks:
                waves.append(ExecutionWave(
                    wave_index=wave_idx,
                    task_ids=[tid],
                    strategy=Strategy.SERIAL,
                ))
                wave_idx += 1

        return waves

    def _topological_layers(
        self, dag: Dict[str, TaskNode]
    ) -> List[List[str]]:
        """Kahn's algorithm variant that groups nodes into layers."""
        in_degree: Dict[str, int] = {tid: 0 for tid in dag}
        for node in dag.values():
            for dep in node.depends_on:
                if dep in in_degree:
                    in_degree[node.id] += 1

        layers: List[List[str]] = []
        remaining = set(dag.keys())

        while remaining:
            # Layer = all nodes with in_degree 0 among remaining
            layer = [tid for tid in remaining if in_degree.get(tid, 0) == 0]
            if not layer:
                # Cycle detected — break tie by putting all remaining in one layer
                layer = list(remaining)
            layers.append(sorted(layer))
            remaining -= set(layer)
            # Decrement in_degree for dependents
            for tid in layer:
                for enabled in dag[tid].enables:
                    if enabled in in_degree:
                        in_degree[enabled] -= 1

        return layers

    def _build_conventions_prompt(
        self, group: List[str], dag: Dict[str, TaskNode]
    ) -> str:
        """Build the conventions instruction for a STAGED_PARALLEL wave.

        Provides risk factors (why drift is likely) and lets the orchestrator
        — which already understands the task domain from planning —
        identify and pin down the specific conventions. Conventions are output
        as text rather than written to a file: in worktree mode workers fork
        the orchestrator's session; in agent-tool mode they reach subagents
        through their Agent prompts.
        """
        task_descs = [f"- {dag[tid].description}" for tid in group]
        tasks_block = '\n'.join(task_descs)
        context = f"\nDomain context: {self.domain_context}" if self.domain_context else ""

        # Build risk-specific guidance from heuristic signals
        risk_hints = self._build_group_risk_hints(group, dag)
        risk_block = '\n'.join(f"  - {h}" for h in risk_hints) if risk_hints else "  - High similarity between task outputs"

        if self.agent_tool:
            return (
                f"BEFORE launching any subagent in this step, output a "
                f"## Conventions section covering all ambiguous decisions.\n\n"
                f"Tasks in this wave:\n{tasks_block}\n{context}\n\n"
                f"The scheduler detected HIGH HIDDEN CONSENSUS RISK because:\n"
                f"{risk_block}\n\n"
                f"Subagents inherit these conventions through their Agent prompts — "
                f"no CONVENTIONS.md file needed. Output conventions as text, include "
                f"them in each Agent prompt, then launch all subagents of this step "
                f"in one response."
            )
        return (
            f"BEFORE launching any worker in this wave, output a "
            f"## Conventions section covering all ambiguous decisions.\n\n"
            f"Tasks in this wave:\n{tasks_block}\n{context}\n\n"
            f"The scheduler detected HIGH HIDDEN CONSENSUS RISK because:\n"
            f"{risk_block}\n\n"
            f"Workers inherit your conversation context via session fork — "
            f"no CONVENTIONS.md file needed. Output conventions as text, "
            f"then launch workers immediately."
        )

    def _build_group_risk_hints(
        self, group: List[str], dag: Dict[str, TaskNode]
    ) -> List[str]:
        """Generate specific risk hints for a group of parallel tasks."""
        hints = []

        # File extension homogeneity
        all_exts: List[str] = []
        for tid in group:
            for f in dag[tid].files:
                if '.' in f:
                    all_exts.append(f.rsplit('.', 1)[-1])
        if all_exts:
            unique_exts = set(all_exts)
            if len(unique_exts) == 1:
                ext = list(unique_exts)[0]
                hints.append(
                    f"All {len(group)} tasks produce .{ext} files — "
                    f"notation, formatting, and style choices will diverge "
                    f"without explicit conventions"
                )

        # Description similarity
        descs = [dag[tid].description for tid in group]
        desc_tokens = [set(_tokenize(d)) for d in descs]
        if len(desc_tokens) >= 2:
            pairwise_sims = []
            for i in range(len(desc_tokens)):
                for j in range(i + 1, len(desc_tokens)):
                    pairwise_sims.append(_jaccard(desc_tokens[i], desc_tokens[j]))
            avg_sim = sum(pairwise_sims) / len(pairwise_sims) if pairwise_sims else 0
            if avg_sim > 0.3:
                hints.append(
                    f"Tasks have high description similarity "
                    f"(avg Jaccard={avg_sim:.2f}) — they produce similar "
                    f"content types with ambiguous style choices"
                )

        # Directory homogeneity
        all_dirs = set()
        for tid in group:
            for f in dag[tid].files:
                if '/' in f:
                    all_dirs.add(f.rsplit('/', 1)[0])
        if len(all_dirs) <= 1 and all_dirs:
            hints.append(
                f"All outputs go to same directory ({list(all_dirs)[0]}) — "
                f"readers will see them side-by-side, inconsistencies are obvious"
            )

        if not hints:
            hints.append("Multiple independent workers producing related outputs")

        return hints

    def _build_risk_factors(
        self, dag: Dict[str, TaskNode], decisions: List[GroupDecision]
    ) -> List[str]:
        """Generate human-readable risk factor summary for the entire schedule."""
        factors = []

        # Overall file type homogeneity
        all_exts: List[str] = []
        for node in dag.values():
            for f in node.files:
                if '.' in f:
                    all_exts.append(f.rsplit('.', 1)[-1])
        if all_exts:
            unique_exts = set(all_exts)
            if len(unique_exts) == 1:
                factors.append(
                    f"All {len(dag)} tasks produce .{list(unique_exts)[0]} files "
                    f"(homogeneity=1.0)"
                )
            elif len(unique_exts) <= 2:
                factors.append(
                    f"Low file type diversity: {unique_exts} across {len(dag)} tasks"
                )

        # Inequality ratios
        parallel_groups = [d for d in decisions if d.strategy == Strategy.STAGED_PARALLEL]
        serial_groups = [d for d in decisions if d.strategy == Strategy.SERIAL and len(d.task_ids) > 1]
        if parallel_groups:
            ratios = [d.ratio for d in parallel_groups]
            factors.append(
                f"{len(parallel_groups)} group(s) parallelized "
                f"(ratio range: {min(ratios):.1f}x-{max(ratios):.1f}x, "
                f"margin: {self.PARALLEL_MARGIN}x)"
            )
        if serial_groups:
            ratios = [d.ratio for d in serial_groups]
            factors.append(
                f"{len(serial_groups)} group(s) serialized "
                f"(ratio range: {min(ratios):.1f}x-{max(ratios):.1f}x, "
                f"below margin {self.PARALLEL_MARGIN}x)"
            )

        # Strategy distribution
        strategies = {}
        for node in dag.values():
            s = node.strategy.value
            strategies[s] = strategies.get(s, 0) + 1
        factors.append(f"Strategy distribution: {strategies}")

        return factors

    def get_risk_factors(self) -> List[str]:
        """Return risk factors generated during the last create_schedule call."""
        return self._risk_factors

    # ---- Public API ----

    def create_schedule(
        self,
        plan_tasks: List[Dict],
        convention_planning_tokens: int = 0,
        layer_convention_tokens: Dict[str, int] = None,
    ) -> Tuple[List[ExecutionWave], List[Dict]]:
        """Main entry point: parse → decide (inequality) → generate waves.

        Pure computation, no LLM calls. Runs in <100ms.

        Args:
            plan_tasks: Task dicts from parsed plan commit.
            convention_planning_tokens: Fallback alignment-cost estimate, used
                for any layer not covered by layer_convention_tokens.
            layer_convention_tokens: Per-layer alignment cost, e.g.
                {"L0": 3000, "L1": 5000}. Each layer uses its own C_align.

        Returns:
            (waves, group_decisions) where group_decisions is a list of
            per-group inequality decision dicts.
        """
        dag = self.build_dag(plan_tasks)
        if not dag:
            return [], []

        decisions = self.decide_strategies(dag, convention_planning_tokens, layer_convention_tokens)
        waves = self.generate_waves(dag)
        self._risk_factors = self._build_risk_factors(dag, decisions)

        return waves, [d.to_dict() for d in decisions]

    @staticmethod
    def get_launchable_tasks(
        waves: List[ExecutionWave],
        current_wave_idx: int,
        completed_tasks: Set[str],
        active_tasks: Set[str],
    ) -> Tuple[List[str], bool]:
        """Determine which tasks can be launched now.

        Returns:
            (launchable_task_ids, wave_advanced)
            wave_advanced is True if we moved to the next wave.
        """
        if current_wave_idx >= len(waves):
            return [], False

        wave = waves[current_wave_idx]
        # Tasks in current wave that haven't been launched or completed
        launchable = [
            tid for tid in wave.task_ids
            if tid not in completed_tasks and tid not in active_tasks
        ]
        return launchable, False

    @staticmethod
    def should_advance_wave(
        waves: List[ExecutionWave],
        current_wave_idx: int,
        completed_tasks: Set[str],
    ) -> bool:
        """Check if all tasks in current wave are done → advance."""
        if current_wave_idx >= len(waves):
            return False
        wave = waves[current_wave_idx]
        return all(tid in completed_tasks for tid in wave.task_ids)

    @staticmethod
    def is_schedule_complete(
        waves: List[ExecutionWave],
        completed_tasks: Set[str],
    ) -> bool:
        """Check if all tasks across all waves are done."""
        all_tasks = set()
        for wave in waves:
            all_tasks.update(wave.task_ids)
        return all_tasks.issubset(completed_tasks)
