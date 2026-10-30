"""Execution efficiency metrics for SquidAgent.

Collects per-worker timing and activity data to quantify whether parallel
execution is actually paying off. Metrics are computed at three levels:

1. Per-Worker   — init/context/exec timings, tool calls, files touched
2. Per-Session  — aggregate speedup, file overlap, overhead ratios
3. Decision     — suggestion for next run (parallel vs sequential)

All time values are UNIX epoch seconds. Durations are seconds.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Literal, Optional, Set, Any


# Phase a worker belongs to in a multi-wave session.
#   first  — initial work pass (wave 1)
#   review — explicit review/audit/verify worker (post-merge consistency check)
#   fix    — repair/patch worker triggered by review findings or orch judgment
WorkerPhase = Literal['first', 'review', 'fix']


# Tools that indicate the worker has started actual implementation work
# (vs. still loading context from MCP/Read).
_IMPLEMENTATION_TOOLS = {'Edit', 'Write', 'NotebookEdit'}

# Cap per-gap time attribution so a single long LLM wait doesn't dominate the
# orchestrator active/idle split. Anything above this is truncated.
_MAX_ORCH_GAP_SECONDS = 120.0

# Matches the ".worktrees/worker-N/" prefix (forward or back slashes) at any
# depth in an absolute path. Used to strip transient worktree locations so the
# same *logical* file can be compared across workers / slot reuses.
_WORKTREE_PREFIX_RE = re.compile(r'^.*?[/\\]\.worktrees[/\\]worker-\d+[/\\]')

# Heuristics for classifying a task branch into first/review/fix.
# Phase 3 Run D produced task names like "task/007-review-drift",
# "task/008-fix-eigen". Phase 3 Run A produced "task/007a-fix-..." etc.
# Order matters: review patterns are checked before fix because a branch like
# "task/007-review-drift-fixes" should still be classified as review.
_REVIEW_KEYWORDS = ('review', 'audit', 'verify', 'consistency-check', 'lint')
_FIX_KEYWORDS = ('fix', 'repair', 'patch', 'correct')


def classify_task_phase(task_branch: Optional[str]) -> WorkerPhase:
    """Best-effort phase classification from a task branch name.

    Returns 'first' as the default — callers that know better (e.g., the
    orchestrator-side wave tracker) can override via WorkerMetrics.phase.
    """
    if not task_branch:
        return 'first'
    name = task_branch.lower()
    # Strip the "task/NNN-" prefix so e.g. "task/007a-fix-..." → "fix-..."
    name = re.sub(r'^task/\d+[a-z]?-', '', name)
    name = re.sub(r'^task/', '', name)
    for kw in _REVIEW_KEYWORDS:
        if kw in name:
            return 'review'
    for kw in _FIX_KEYWORDS:
        if kw in name:
            return 'fix'
    return 'first'


def _normalize_worktree_path(p: str) -> str:
    """Strip ``.worktrees/worker-N/`` prefix to get the logical repo path.

    Why: file_overlap_index needs to answer "do two workers touch the same
    logical file?" — not "do they touch the same absolute path?". Without
    normalization:
      - Parallel mode gets phantom independence (worker-1/models/user.py
        and worker-2/models/user.py look distinct even though they merge to
        the same main-tree file).
      - Serial mode gets phantom overlap (all 6 tasks reuse
        .worktrees/worker-1/, inflating Jaccard even for disjoint files).
    """
    return _WORKTREE_PREFIX_RE.sub('', p)


@dataclass
class WorkerMetrics:
    """Metrics for a single worker execution."""

    worker_id: str
    task_branch: str

    # Four key timestamps (epoch seconds)
    t_launch: float = 0.0
    t_first_message: Optional[float] = None
    t_first_edit: Optional[float] = None
    t_complete: Optional[float] = None

    # Activity counters
    tool_calls: Dict[str, int] = field(default_factory=dict)
    # All files referenced by any tool (Read, Edit, Write, etc.).
    files_touched: Set[str] = field(default_factory=set)
    # Subset of files_touched that were actually modified (Edit/Write/
    # NotebookEdit). This is the real conflict surface for FOI.
    files_written: Set[str] = field(default_factory=set)
    message_count: int = 0

    # Result
    exit_code: Optional[int] = None
    error_message: Optional[str] = None

    # Phase classification (first / review / fix). Filled at launch time
    # from task_branch heuristic; can be overridden by upstream callers that
    # know the wave structure better.
    phase: WorkerPhase = 'first'

    # Token accounting from the SDK ResultMessage. Aggregated per worker so
    # that session-level "economic drift tax" (tokens_fix / tokens_first)
    # can be computed alongside the time-based symbol drift tax. Populated
    # by record_worker_complete via the optional usage payload.
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_creation: int = 0
    cost_usd: float = 0.0

    # --- Derived durations ---

    @property
    def C_init(self) -> float:
        """SDK initialization cost: launch -> first message."""
        if self.t_first_message is not None:
            return self.t_first_message - self.t_launch
        return 0.0

    @property
    def C_context(self) -> float:
        """Context reconstruction cost: first message -> first edit.

        This is the time the worker spends reading metadata, plans, existing
        code etc. before producing its first change. A high value suggests
        the orchestrator should pre-load context in the instructions.
        """
        if self.t_first_edit is not None and self.t_first_message is not None:
            return self.t_first_edit - self.t_first_message
        return 0.0

    @property
    def t_exec(self) -> float:
        """Pure execution time: first edit -> complete."""
        if self.t_complete is not None and self.t_first_edit is not None:
            return self.t_complete - self.t_first_edit
        return 0.0

    @property
    def t_total(self) -> float:
        """End-to-end wall clock time for this worker."""
        if self.t_complete is not None:
            return self.t_complete - self.t_launch
        return 0.0

    @property
    def overhead_ratio(self) -> float:
        """(C_init + C_context) / t_total.

        Fraction of worker time spent on overhead rather than implementation.
        Values > 0.5 indicate the task is too small to benefit from parallelism.
        """
        total = self.t_total
        if total <= 0:
            return 0.0
        return (self.C_init + self.C_context) / total

    @property
    def tokens_total(self) -> int:
        """Total billable tokens (input + output, excluding cache reads).

        cache_read tokens are counted separately because they are heavily
        discounted by the API; including them would inflate the apparent
        token cost of cheap context-heavy workers.
        """
        return self.tokens_input + self.tokens_output

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['files_touched'] = sorted(self.files_touched)
        d['files_written'] = sorted(self.files_written)
        d['derived'] = {
            'C_init': round(self.C_init, 2),
            'C_context': round(self.C_context, 2),
            't_exec': round(self.t_exec, 2),
            't_total': round(self.t_total, 2),
            'overhead_ratio': round(self.overhead_ratio, 3),
            'tokens_total': self.tokens_total,
        }
        return d


@dataclass
class SessionMetrics:
    """Metrics for a full development session."""

    session_id: str
    max_parallel: int
    t_start: float = 0.0
    t_end: Optional[float] = None
    workers: Dict[str, WorkerMetrics] = field(default_factory=dict)

    # --- Orchestrator tracking ---
    # Tools the orchestrator called itself (not via a Worker).
    orchestrator_tool_calls: Dict[str, int] = field(default_factory=dict)
    # Files the orchestrator directly read/wrote/edited.
    orchestrator_files_touched: Set[str] = field(default_factory=set)
    # Count of orchestrator AssistantMessage events.
    orchestrator_message_count: int = 0
    # Time the orchestrator was producing events while NO worker was active.
    orchestrator_active_time: float = 0.0
    # Time the orchestrator was silent AND at least one worker was running.
    orchestrator_idle_time: float = 0.0
    # Breakdown of orchestrator ACTIVE time into alignment-cost phases (the
    # empirical Ĉ_alg the scheduler models but the wave view can't see):
    #   conventions  = writing the shared ## Conventions block / CONVENTIONS.md
    #   serial_write = writing SERIAL-decided tasks directly (Write)
    #   reconcile    = post-merge cross-file consistency pass (Grep + Edit)
    #   other        = planning, launch/finalize overhead, reads, thinking
    # Sum ≈ orchestrator_active_time. Classified per AssistantMessage in cli.py.
    orchestrator_phase_time: Dict[str, float] = field(
        default_factory=lambda: {
            'conventions': 0.0, 'serial_write': 0.0,
            'reconcile': 0.0, 'other': 0.0,
        }
    )
    # Token usage for the orchestrator's own SDKClient session.
    orchestrator_tokens_input: int = 0
    orchestrator_tokens_output: int = 0
    orchestrator_tokens_cache_read: int = 0
    orchestrator_tokens_cache_creation: int = 0
    orchestrator_cost_usd: float = 0.0
    # Internal: timestamp of the last orchestrator event (not serialized).
    _last_orch_event_ts: Optional[float] = field(default=None, repr=False)
    # Internal: monotonically increasing counter used to make worker storage
    # keys unique across sequential re-uses of the same slot. Without this,
    # max_parallel=1 runs overwrite the single slot's metrics on every launch
    # and only the last worker survives.
    _launch_seq: int = field(default=0, repr=False)
    # Internal: maps worker_id (slot number) -> current storage key. Used by
    # _get_worker() so that record_first_message / record_tool_call /
    # record_worker_complete continue to accept the bare worker_id.
    _active_slot_keys: Dict[str, str] = field(default_factory=dict, repr=False)
    # Wave tracking. A "wave" is a contiguous group of workers that ran in
    # parallel without any orchestrator wave-transition phase between them.
    # We detect waves by looking at gaps in launch timestamps: if a new
    # worker launches while at least one prior worker from the same wave is
    # still running, it joins that wave; otherwise it opens a new wave.
    #
    # wave_boundaries holds (wave_index, t_first_launch_in_wave,
    # t_last_complete_in_wave_so_far). It is rebuilt lazily by
    # _compute_waves() — recording is append-only and lock-free.
    _wave_membership: Dict[str, int] = field(default_factory=dict, repr=False)
    # Cumulative wave-transition time, computed lazily. Defined as the sum
    # of (t_first_launch[wave_k+1] - t_last_complete[wave_k]) for all
    # k >= 1. This is the gap between "the last worker of one wave finishes"
    # and "the first worker of the next wave starts" — i.e., the orchestrator
    # serial overhead between waves. See phase3_symbol_drift.md §6.2 L3.
    _wave_transition_cached: Optional[float] = field(default=None, repr=False)

    # --- Aggregate durations ---

    @property
    def T_total(self) -> float:
        """Actual wall clock time of the whole session."""
        if self.t_end is not None:
            return self.t_end - self.t_start
        return 0.0

    @property
    def T_seq_estimated(self) -> float:
        """Estimated serial time = sum of all worker t_total.

        This approximates what a sequential run would take IF each task
        had the same per-task cost. Underestimates if parallel runs
        duplicated work (e.g., repeated context loading).
        """
        return sum(w.t_total for w in self.workers.values())

    @property
    def speedup(self) -> float:
        """Observed speedup: T_seq_estimated / T_total.

        > 1.0 = parallel helped
        ~1.0 = no benefit
        < 1.0 = parallel was counterproductive (rare — only if orchestrator
                overhead dominates)
        """
        if self.T_total <= 0:
            return 0.0
        return self.T_seq_estimated / self.T_total

    @property
    def theoretical_max_speedup(self) -> float:
        """Best possible speedup given this max_parallel setting."""
        return float(self.max_parallel) if self.max_parallel > 0 else 1.0

    @property
    def parallel_efficiency(self) -> float:
        """speedup / max_parallel.

        1.0 means workers ran with zero overhead (impossible in practice).
        < 0.5 means the system is wasting most of the parallelism.
        """
        if self.max_parallel <= 0:
            return 0.0
        return self.speedup / self.max_parallel

    @staticmethod
    def _foi_for(workers: List['WorkerMetrics']) -> float:
        """Average Jaccard overlap of files written across the given workers."""
        normalized: List[Set[str]] = []
        for w in workers:
            if not w.files_written:
                continue
            normalized.append(
                {_normalize_worktree_path(p) for p in w.files_written}
            )

        if len(normalized) < 2:
            return 0.0

        overlaps: List[float] = []
        for i in range(len(normalized)):
            for j in range(i + 1, len(normalized)):
                a = normalized[i]
                b = normalized[j]
                union = a | b
                if union:
                    overlaps.append(len(a & b) / len(union))

        if not overlaps:
            return 0.0
        return sum(overlaps) / len(overlaps)

    @property
    def file_overlap_index(self) -> float:
        """Average Jaccard overlap of files WRITTEN across worker pairs.

        Uses files_written (not files_touched) because only writes cause
        conflicts — two workers reading the same file is fine. Paths are
        normalized via _normalize_worktree_path so worktree prefixes don't
        create phantom independence (parallel) or phantom overlap (serial
        slot reuse).

        Returns 0.0 if fewer than 2 workers wrote any files.

        See ``first_pass_foi`` / ``review_foi`` / ``fix_pass_foi`` for the
        per-phase split — Phase 3 found that mixing all phases together
        masks "review worker reads all chapters" and similar legitimate
        cross-cuts that aren't real conflicts.
        """
        return self._foi_for(list(self.workers.values()))

    @property
    def first_pass_foi(self) -> float:
        """FOI restricted to first-pass workers."""
        return self._foi_for(
            [w for w in self.workers.values() if w.phase == 'first']
        )

    @property
    def review_foi(self) -> float:
        """FOI restricted to review workers (almost always 0 — review workers
        rarely write the chapter files; they write a drift report)."""
        return self._foi_for(
            [w for w in self.workers.values() if w.phase == 'review']
        )

    @property
    def fix_pass_foi(self) -> float:
        """FOI restricted to fix-pass workers."""
        return self._foi_for(
            [w for w in self.workers.values() if w.phase == 'fix']
        )

    @property
    def avg_overhead_ratio(self) -> float:
        """Average overhead ratio across all workers."""
        ratios = [w.overhead_ratio for w in self.workers.values() if w.t_total > 0]
        if not ratios:
            return 0.0
        return sum(ratios) / len(ratios)

    @property
    def avg_C_init(self) -> float:
        vals = [w.C_init for w in self.workers.values() if w.C_init > 0]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def avg_C_context(self) -> float:
        vals = [w.C_context for w in self.workers.values() if w.C_context > 0]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def avg_t_exec(self) -> float:
        vals = [w.t_exec for w in self.workers.values() if w.t_exec > 0]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def orchestrator_utilization(self) -> float:
        """active / (active + idle).

        1.0 = orchestrator never waited on a worker (all time spent working).
        0.0 = orchestrator only waited on workers.
        Useful to answer: "Is the orchestrator actually blocking on workers,
        or is it doing most of the work itself?"
        """
        total = self.orchestrator_active_time + self.orchestrator_idle_time
        if total <= 0:
            return 0.0
        return self.orchestrator_active_time / total

    @property
    def success_rate(self) -> float:
        total = len(self.workers)
        if total == 0:
            return 0.0
        ok = sum(1 for w in self.workers.values() if w.exit_code == 0)
        return ok / total

    # --- Phase aggregates (Phase 3 §6.2 L1/L2/L3 instrumentation) ---

    def _workers_by_phase(self, phase: WorkerPhase) -> List[WorkerMetrics]:
        return [w for w in self.workers.values() if w.phase == phase]

    @property
    def first_pass_t_total_sum(self) -> float:
        return sum(w.t_total for w in self._workers_by_phase('first'))

    @property
    def review_t_total_sum(self) -> float:
        return sum(w.t_total for w in self._workers_by_phase('review'))

    @property
    def fix_pass_t_total_sum(self) -> float:
        return sum(w.t_total for w in self._workers_by_phase('fix'))

    @property
    def symbol_drift_tax(self) -> float:
        """sum(fix t_total) / sum(first t_total).

        See [phase3_symbol_drift.md F4](../experiments/phase3_symbol_drift.md).
        Returns 0.0 if there are no first-pass workers.
        """
        denom = self.first_pass_t_total_sum
        if denom <= 0:
            return 0.0
        return self.fix_pass_t_total_sum / denom

    @property
    def first_pass_tokens(self) -> int:
        return sum(w.tokens_total for w in self._workers_by_phase('first'))

    @property
    def review_tokens(self) -> int:
        return sum(w.tokens_total for w in self._workers_by_phase('review'))

    @property
    def fix_pass_tokens(self) -> int:
        return sum(w.tokens_total for w in self._workers_by_phase('fix'))

    @property
    def economic_drift_tax(self) -> float:
        """tokens(fix) / tokens(first) — token-domain analogue of symbol_drift_tax.

        Cleaner than the time-domain version because it isn't polluted by
        wave transition gaps or LLM thinking pauses.
        """
        denom = self.first_pass_tokens
        if denom <= 0:
            return 0.0
        return self.fix_pass_tokens / denom

    # --- Wave detection (Phase 3 §6.2 L3 instrumentation) ---

    def _compute_waves(self) -> List[Dict[str, Any]]:
        """Group workers into waves and compute per-wave timing.

        Algorithm: walk workers in launch-time order. A worker joins the
        current wave if it launched while at least one earlier wave member
        was still running. Otherwise it opens a new wave. This matches the
        intuition: a wave is a contiguous burst of overlapping workers.

        Returns a list of dicts with keys:
            wave_index, n_workers, t_first_launch, t_last_complete,
            duration, max_worker_t_total, worker_keys
        """
        # Sort by launch time, then storage key for stable ordering when
        # multiple workers launch in the same millisecond.
        ordered = sorted(
            self.workers.items(),
            key=lambda kv: (kv[1].t_launch, kv[0]),
        )

        waves: List[Dict[str, Any]] = []
        for storage_key, w in ordered:
            if w.t_launch <= 0 or w.t_complete is None:
                # Skip workers that never launched or never completed —
                # we can't place them in a wave reliably.
                continue
            if not waves:
                waves.append({
                    'wave_index': 1,
                    'n_workers': 1,
                    't_first_launch': w.t_launch,
                    't_last_complete': w.t_complete,
                    'max_worker_t_total': w.t_total,
                    'worker_keys': [storage_key],
                })
                self._wave_membership[storage_key] = 1
                continue

            current = waves[-1]
            if w.t_launch < current['t_last_complete']:
                # Joined the current wave (overlap with at least one member).
                current['n_workers'] += 1
                current['t_last_complete'] = max(
                    current['t_last_complete'], w.t_complete
                )
                current['max_worker_t_total'] = max(
                    current['max_worker_t_total'], w.t_total
                )
                current['worker_keys'].append(storage_key)
                self._wave_membership[storage_key] = current['wave_index']
            else:
                # Opened a new wave.
                next_index = current['wave_index'] + 1
                waves.append({
                    'wave_index': next_index,
                    'n_workers': 1,
                    't_first_launch': w.t_launch,
                    't_last_complete': w.t_complete,
                    'max_worker_t_total': w.t_total,
                    'worker_keys': [storage_key],
                })
                self._wave_membership[storage_key] = next_index

        for wv in waves:
            wv['duration'] = round(
                wv['t_last_complete'] - wv['t_first_launch'], 2
            )

        return waves

    @property
    def waves(self) -> List[Dict[str, Any]]:
        return self._compute_waves()

    @property
    def n_waves(self) -> int:
        return len(self._compute_waves())

    @property
    def wave_transition_time(self) -> float:
        """Sum of gaps between consecutive waves.

        For each k >= 1, gap_k = t_first_launch[k+1] - t_last_complete[k].
        Sum across all k. This is the L3 cost from Phase 3 §6.2 — the
        orchestrator's serial wave-transition overhead (parse_worker_commit
        + update_plan_branch + remove_worktree + create_task_branch +
        create_worktree + launch_worker_async + LLM round-trips), measured
        directly instead of reverse-engineered from tool call counts.

        Returns 0.0 for single-wave (or zero-wave) sessions.
        """
        if self._wave_transition_cached is not None:
            return self._wave_transition_cached
        wvs = self._compute_waves()
        if len(wvs) < 2:
            self._wave_transition_cached = 0.0
            return 0.0
        total = 0.0
        for prev, curr in zip(wvs, wvs[1:]):
            gap = curr['t_first_launch'] - prev['t_last_complete']
            if gap > 0:
                total += gap
        self._wave_transition_cached = round(total, 2)
        return self._wave_transition_cached

    @property
    def avg_wave_transition_time(self) -> float:
        """Mean wave-transition gap across the (n_waves - 1) transitions.

        Useful for comparing different sessions with different wave counts.
        """
        wvs = self._compute_waves()
        if len(wvs) < 2:
            return 0.0
        return self.wave_transition_time / (len(wvs) - 1)

    # --- Decision support ---

    def recommendation(self) -> Dict[str, Any]:
        """Suggest whether to use parallel or sequential next time.

        Based on the thresholds discussed in the design:
          - speedup > 1.5           -> parallel was beneficial
          - overhead_ratio < 0.3    -> tasks large enough for parallelism
          - file_overlap < 0.2      -> tasks sufficiently independent
        """
        reasons: List[str] = []
        should_parallel = True

        if self.speedup < 1.2:
            should_parallel = False
            reasons.append(
                f"Low speedup ({self.speedup:.2f}x) — parallel did not meaningfully help"
            )

        if self.avg_overhead_ratio > 0.4:
            should_parallel = False
            reasons.append(
                f"High overhead ratio ({self.avg_overhead_ratio:.0%}) — tasks too small; "
                f"init+context costs dominate execution"
            )

        if self.file_overlap_index > 0.2:
            should_parallel = False
            reasons.append(
                f"High file overlap ({self.file_overlap_index:.0%}) — tasks not "
                f"independent; merge conflict risk"
            )

        if self.success_rate < 1.0:
            reasons.append(
                f"Worker failure rate: {(1 - self.success_rate):.0%} — investigate errors"
            )

        if not reasons:
            reasons.append("Parallel execution was efficient — keep current settings")

        return {
            'recommend_parallel': should_parallel,
            'suggested_max_parallel': (
                self.max_parallel if should_parallel else 1
            ),
            'reasons': reasons,
        }

    # --- Serialization ---

    def to_dict(self) -> Dict[str, Any]:
        # Compute waves once so phase membership is populated and the
        # wave-transition cache is filled before serialization.
        waves = self._compute_waves()
        # Reset cache so future writes (after more workers complete) recompute.
        # phase counts come from worker.phase, which is set at launch.
        n_first = sum(1 for w in self.workers.values() if w.phase == 'first')
        n_review = sum(1 for w in self.workers.values() if w.phase == 'review')
        n_fix = sum(1 for w in self.workers.values() if w.phase == 'fix')

        return {
            'session_id': self.session_id,
            'max_parallel': self.max_parallel,
            't_start': self.t_start,
            't_end': self.t_end,
            'aggregate': {
                'T_total': round(self.T_total, 2),
                'T_seq_estimated': round(self.T_seq_estimated, 2),
                'speedup': round(self.speedup, 3),
                'theoretical_max_speedup': self.theoretical_max_speedup,
                'parallel_efficiency': round(self.parallel_efficiency, 3),
                'file_overlap_index': round(self.file_overlap_index, 3),
                'first_pass_foi': round(self.first_pass_foi, 3),
                'review_foi': round(self.review_foi, 3),
                'fix_pass_foi': round(self.fix_pass_foi, 3),
                'avg_overhead_ratio': round(self.avg_overhead_ratio, 3),
                'avg_C_init': round(self.avg_C_init, 2),
                'avg_C_context': round(self.avg_C_context, 2),
                'avg_t_exec': round(self.avg_t_exec, 2),
                'success_rate': round(self.success_rate, 3),
                'n_workers': len(self.workers),
                'n_workers_first': n_first,
                'n_workers_review': n_review,
                'n_workers_fix': n_fix,
            },
            'phase_breakdown': {
                'first_pass_t_total_sum': round(self.first_pass_t_total_sum, 2),
                'review_t_total_sum': round(self.review_t_total_sum, 2),
                'fix_pass_t_total_sum': round(self.fix_pass_t_total_sum, 2),
                'symbol_drift_tax': round(self.symbol_drift_tax, 3),
                'first_pass_tokens': self.first_pass_tokens,
                'review_tokens': self.review_tokens,
                'fix_pass_tokens': self.fix_pass_tokens,
                'economic_drift_tax': round(self.economic_drift_tax, 3),
            },
            'waves': {
                'n_waves': len(waves),
                'wave_transition_time': self.wave_transition_time,
                'avg_wave_transition_time': round(
                    self.avg_wave_transition_time, 2
                ),
                'detail': [
                    {
                        'wave_index': wv['wave_index'],
                        'n_workers': wv['n_workers'],
                        't_first_launch': round(wv['t_first_launch'], 2),
                        't_last_complete': round(wv['t_last_complete'], 2),
                        'duration': wv['duration'],
                        'max_worker_t_total': round(
                            wv['max_worker_t_total'], 2
                        ),
                        'worker_keys': wv['worker_keys'],
                    }
                    for wv in waves
                ],
            },
            'orchestrator': {
                'active_time': round(self.orchestrator_active_time, 2),
                'idle_time': round(self.orchestrator_idle_time, 2),
                # Ĉ_alg breakdown of active_time (conventions/serial_write/reconcile/other).
                'phase_time': {k: round(v, 2) for k, v in self.orchestrator_phase_time.items()},
                'utilization': round(self.orchestrator_utilization, 3),
                'message_count': self.orchestrator_message_count,
                'tool_calls': dict(self.orchestrator_tool_calls),
                'total_tool_calls': sum(self.orchestrator_tool_calls.values()),
                'files_touched': sorted(self.orchestrator_files_touched),
                'tokens_input': self.orchestrator_tokens_input,
                'tokens_output': self.orchestrator_tokens_output,
                'tokens_cache_read': self.orchestrator_tokens_cache_read,
                'tokens_cache_creation': self.orchestrator_tokens_cache_creation,
                'tokens_total': (
                    self.orchestrator_tokens_input
                    + self.orchestrator_tokens_output
                ),
                'cost_usd': round(self.orchestrator_cost_usd, 4),
            },
            'workers': {
                wid: w.to_dict() for wid, w in self.workers.items()
            },
            'recommendation': self.recommendation(),
        }

    def format_report(self) -> str:
        """Generate human-readable report for console output."""
        d = self.to_dict()
        agg = d['aggregate']
        rec = d['recommendation']

        lines = []
        lines.append("=" * 70)
        lines.append(f"SquidAgent Execution Metrics — {self.session_id}")
        lines.append("=" * 70)

        lines.append("")
        lines.append("## Session Aggregate")
        lines.append(f"  Total wall time       : {agg['T_total']:>8.2f}s")
        lines.append(f"  Estimated serial time : {agg['T_seq_estimated']:>8.2f}s")
        lines.append(f"  Speedup               : {agg['speedup']:>8.2f}x  "
                     f"(max possible: {agg['theoretical_max_speedup']:.1f}x)")
        lines.append(f"  Parallel efficiency   : {agg['parallel_efficiency']:>8.1%}")
        lines.append(f"  Workers               : {agg['n_workers']}  "
                     f"(max_parallel={self.max_parallel})")
        lines.append(f"  Success rate          : {agg['success_rate']:>8.1%}")

        lines.append("")
        lines.append("## Overhead Breakdown")
        lines.append(f"  Avg init cost  (C_init)    : {agg['avg_C_init']:>8.2f}s")
        lines.append(f"  Avg context cost (C_ctx)   : {agg['avg_C_context']:>8.2f}s")
        lines.append(f"  Avg pure exec  (t_exec)    : {agg['avg_t_exec']:>8.2f}s")
        lines.append(f"  Avg overhead ratio         : {agg['avg_overhead_ratio']:>8.1%}")
        lines.append(f"  File overlap index (FOI)   : {agg['file_overlap_index']:>8.1%}")
        lines.append(f"    └ first-pass FOI         : {agg['first_pass_foi']:>8.1%}")
        lines.append(f"    └ review FOI             : {agg['review_foi']:>8.1%}")
        lines.append(f"    └ fix-pass FOI           : {agg['fix_pass_foi']:>8.1%}")

        # Phase breakdown — only show if there is more than one phase present
        # (single-phase sessions are usually plain Phase 2-style first-pass-only).
        pb = d.get('phase_breakdown', {})
        n_review = agg.get('n_workers_review', 0)
        n_fix = agg.get('n_workers_fix', 0)
        if n_review > 0 or n_fix > 0:
            lines.append("")
            lines.append("## Phase Breakdown (first / review / fix)")
            lines.append(
                f"  Workers                    : "
                f"{agg['n_workers_first']} first / "
                f"{n_review} review / {n_fix} fix"
            )
            lines.append(
                f"  sum(t_total) per phase     : "
                f"{pb['first_pass_t_total_sum']:>7.1f}s / "
                f"{pb['review_t_total_sum']:>6.1f}s / "
                f"{pb['fix_pass_t_total_sum']:>6.1f}s"
            )
            lines.append(
                f"  Symbol drift tax           : "
                f"{pb['symbol_drift_tax']:>8.1%}  (sum(fix)/sum(first))"
            )
            if pb['first_pass_tokens'] > 0:
                lines.append(
                    f"  Tokens per phase (in+out)  : "
                    f"{pb['first_pass_tokens']:>7d} / "
                    f"{pb['review_tokens']:>6d} / "
                    f"{pb['fix_pass_tokens']:>6d}"
                )
                lines.append(
                    f"  Economic drift tax         : "
                    f"{pb['economic_drift_tax']:>8.1%}  (tokens(fix)/tokens(first))"
                )

        # Wave breakdown — show if more than one wave detected
        wv = d.get('waves', {})
        if wv.get('n_waves', 0) > 1:
            lines.append("")
            lines.append("## Wave Structure (Phase 3 §6.2 L3)")
            lines.append(f"  Wave count                 : {wv['n_waves']:>8d}")
            lines.append(
                f"  Wave-transition gap (sum)  : "
                f"{wv['wave_transition_time']:>8.2f}s  "
                f"(serial overhead between waves)"
            )
            lines.append(
                f"  Avg gap per transition     : "
                f"{wv['avg_wave_transition_time']:>8.2f}s"
            )
            for w_detail in wv['detail']:
                lines.append(
                    f"    wave {w_detail['wave_index']}: "
                    f"n={w_detail['n_workers']:<2d} "
                    f"dur={w_detail['duration']:>6.1f}s  "
                    f"max_worker={w_detail['max_worker_t_total']:>6.1f}s"
                )

        lines.append("")
        lines.append("## Orchestrator Activity")
        orch = d['orchestrator']
        lines.append(f"  Active time (own work)    : {orch['active_time']:>8.2f}s")
        lines.append(f"  Idle time (waiting)       : {orch['idle_time']:>8.2f}s")
        lines.append(f"  Utilization               : {orch['utilization']:>8.1%}")
        lines.append(f"  Messages                  : {orch['message_count']:>8d}")
        lines.append(f"  Tool calls (total)        : {orch['total_tool_calls']:>8d}")
        lines.append(f"  Files touched directly    : {len(orch['files_touched']):>8d}")
        if orch['tool_calls']:
            top_tools = sorted(
                orch['tool_calls'].items(), key=lambda kv: -kv[1]
            )[:5]
            tool_str = ", ".join(f"{n}={c}" for n, c in top_tools)
            lines.append(f"  Top tools                 : {tool_str}")

        lines.append("")
        lines.append("## Per-Worker Detail")
        lines.append(f"  {'KEY':<8} {'phase':<6} {'task':<24} {'t_total':>8} {'C_init':>8} "
                     f"{'C_ctx':>8} {'t_exec':>8} {'tools':>6} {'files':>6} {'status':>8}")
        lines.append(f"  {'-'*8} {'-'*6} {'-'*24} {'-'*8} {'-'*8} {'-'*8} {'-'*8} "
                     f"{'-'*6} {'-'*6} {'-'*8}")
        for wid, w in sorted(self.workers.items()):
            status = 'ok' if w.exit_code == 0 else f'err({w.exit_code})'
            n_tools = sum(w.tool_calls.values())
            task_disp = w.task_branch or ""
            if len(task_disp) > 24:
                task_disp = task_disp[:21] + "..."
            lines.append(
                f"  {wid:<8} "
                f"{w.phase:<6} "
                f"{task_disp:<24} "
                f"{w.t_total:>7.1f}s "
                f"{w.C_init:>7.1f}s "
                f"{w.C_context:>7.1f}s "
                f"{w.t_exec:>7.1f}s "
                f"{n_tools:>6} "
                f"{len(w.files_touched):>6} "
                f"{status:>8}"
            )

        lines.append("")
        lines.append("## Recommendation")
        mode = "PARALLEL" if rec['recommend_parallel'] else "SEQUENTIAL"
        lines.append(f"  Next run: {mode}  "
                     f"(suggested max_parallel={rec['suggested_max_parallel']})")
        for reason in rec['reasons']:
            lines.append(f"    • {reason}")

        lines.append("=" * 70)
        return "\n".join(lines)

    def save_json(self, output_dir: Optional[str] = None) -> str:
        """Save metrics as JSON to .squidagent/metrics/ directory.

        Writes atomically (temp file + os.replace). On success, removes any
        leftover ``<session_id>.partial.json`` checkpoint.

        Returns the written file path.
        """
        if output_dir is None:
            output_dir = os.path.join(os.getcwd(), '.squidagent', 'metrics')
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        fname = f"{self.session_id}.json"
        fpath = os.path.join(output_dir, fname)
        tmp_path = fpath + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        os.replace(tmp_path, fpath)

        # Drop the partial checkpoint — the final file supersedes it.
        partial_path = os.path.join(
            output_dir, f"{self.session_id}.partial.json"
        )
        if os.path.exists(partial_path):
            try:
                os.remove(partial_path)
            except OSError:
                pass

        return fpath

    def save_partial(self, output_dir: Optional[str] = None) -> str:
        """Write a mid-session checkpoint to ``<session_id>.partial.json``.

        Called from record_worker_complete so that crashes, kills, or a
        forgotten ``/exit`` never wipe the accumulated metrics. Uses atomic
        temp+rename so a partial file on disk is always consistent.

        If ``t_end`` is unset (session still live), a virtual end time of
        ``time.time()`` is used for the snapshot and then rolled back, so
        the running session is not mutated.
        """
        if output_dir is None:
            output_dir = os.path.join(os.getcwd(), '.squidagent', 'metrics')
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        # Snapshot with a virtual t_end so T_total/speedup reflect progress
        # so far — without committing a fake t_end on the live session.
        saved_t_end = self.t_end
        if self.t_end is None:
            self.t_end = time.time()
        try:
            data = self.to_dict()
        finally:
            self.t_end = saved_t_end

        fname = f"{self.session_id}.partial.json"
        fpath = os.path.join(output_dir, fname)
        tmp_path = fpath + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, fpath)
        return fpath


class MetricsCollector:
    """Singleton collector — records events from sdk_workers + cli.

    Event methods are fire-and-forget: if no session is active, they silently
    no-op so that instrumentation never breaks production flow.
    """

    def __init__(self) -> None:
        self.current_session: Optional[SessionMetrics] = None

    # --- Session lifecycle ---

    def start_session(self, session_id: str, max_parallel: int) -> None:
        self.current_session = SessionMetrics(
            session_id=session_id,
            max_parallel=max_parallel,
            t_start=time.time(),
        )

    def end_session(self) -> Optional[SessionMetrics]:
        if self.current_session is None:
            return None
        s = self.current_session
        s.t_end = time.time()

        # Attribute the trailing gap (between last orchestrator event and
        # session end) to active/idle. If the orchestrator never produced an
        # event (unusual), attribute nothing.
        if s._last_orch_event_ts is not None:
            self._attribute_gap(s, s._last_orch_event_ts, s.t_end)
            s._last_orch_event_ts = s.t_end

        finished = s
        # Keep the finished session accessible until the next start_session
        return finished

    @staticmethod
    def _any_worker_active_between(
        session: SessionMetrics, a: float, b: float
    ) -> bool:
        """True if any worker's [t_launch, t_complete] interval overlaps [a, b]."""
        if b <= a:
            return False
        for w in session.workers.values():
            if w.t_launch <= 0:
                continue
            end = w.t_complete if w.t_complete is not None else b
            if w.t_launch < b and end > a:
                return True
        return False

    def _attribute_gap(
        self, session: SessionMetrics, start: float, end: float,
        phase: str = 'other',
    ) -> None:
        """Add the interval [start, end] to orchestrator active OR idle time.

        Idle = at least one worker was running during the gap (orchestrator
        was waiting). Active = no worker running (orchestrator was thinking
        or doing tool calls on its own).

        When the gap is ACTIVE, it is also added to the given phase bucket so
        active time breaks down into the alignment-cost phases (conventions /
        serial_write / reconcile / other).

        Gaps longer than _MAX_ORCH_GAP_SECONDS are capped, to prevent a single
        hung LLM call from dominating the numbers.
        """
        delta = end - start
        if delta <= 0:
            return
        if delta > _MAX_ORCH_GAP_SECONDS:
            delta = _MAX_ORCH_GAP_SECONDS
        if self._any_worker_active_between(session, start, end):
            session.orchestrator_idle_time += delta
        else:
            session.orchestrator_active_time += delta
            if phase not in session.orchestrator_phase_time:
                phase = 'other'
            session.orchestrator_phase_time[phase] += delta

    # --- Worker events ---

    def record_worker_launch(self, worker_id: str, task_branch: str) -> None:
        if self.current_session is None:
            return
        s = self.current_session

        # Unique storage key per launch: "{worker_id}-{seq:03d}" e.g. "1-001".
        # Required for serial runs where the same slot is reused many times.
        s._launch_seq += 1
        storage_key = f"{worker_id}-{s._launch_seq:03d}"

        s.workers[storage_key] = WorkerMetrics(
            worker_id=worker_id,
            task_branch=task_branch,
            t_launch=time.time(),
            phase=classify_task_phase(task_branch),
        )
        # Remember which storage key belongs to this slot so _get_worker()
        # can resolve a bare worker_id back to the currently-active record.
        s._active_slot_keys[worker_id] = storage_key
        # Invalidate wave cache — a new launch may extend an existing wave or
        # open a new one.
        s._wave_transition_cached = None

    def record_first_message(self, worker_id: str) -> None:
        w = self._get_worker(worker_id)
        if w is not None and w.t_first_message is None:
            w.t_first_message = time.time()

    def record_tool_call(
        self,
        worker_id: str,
        tool_name: Optional[str],
        tool_input: Optional[Dict[str, Any]] = None,
    ) -> None:
        w = self._get_worker(worker_id)
        if w is None:
            return

        w.message_count += 1

        if not tool_name:
            return

        w.tool_calls[tool_name] = w.tool_calls.get(tool_name, 0) + 1

        # Mark first implementation action
        if tool_name in _IMPLEMENTATION_TOOLS and w.t_first_edit is None:
            w.t_first_edit = time.time()

        # Track files touched (best effort — tool_input shape varies).
        # Write tools also populate files_written, which is the conflict
        # surface used by file_overlap_index.
        if isinstance(tool_input, dict):
            for key in ('file_path', 'notebook_path', 'path'):
                val = tool_input.get(key)
                if isinstance(val, str):
                    w.files_touched.add(val)
                    if tool_name in _IMPLEMENTATION_TOOLS:
                        w.files_written.add(val)
                    break

    def record_worker_complete(
        self,
        worker_id: str,
        exit_code: int,
        error_message: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        cost_usd: Optional[float] = None,
    ) -> None:
        w = self._get_worker(worker_id)
        if w is None:
            return
        if w.t_complete is None:
            w.t_complete = time.time()
        w.exit_code = exit_code
        if error_message:
            w.error_message = error_message
        if usage is not None:
            self._apply_usage(w, usage)
        if cost_usd is not None:
            w.cost_usd = float(cost_usd)

        # Invalidate wave cache — completion changes the wave's t_last_complete.
        if self.current_session is not None:
            self.current_session._wave_transition_cached = None

        # Checkpoint: flush a partial snapshot so crashes / kills / forgotten
        # /exit don't wipe everything we've recorded. Fire-and-forget; must
        # never break the session on disk errors.
        if self.current_session is not None:
            try:
                self.current_session.save_partial()
            except Exception:
                pass

    def record_worker_usage(
        self,
        worker_id: str,
        usage: Dict[str, Any],
        cost_usd: Optional[float] = None,
    ) -> None:
        """Update token counters for an in-progress or just-completed worker.

        Called from sdk_workers.py when the worker's SDK ResultMessage arrives.
        Safe to call multiple times: token fields accumulate.
        """
        w = self._get_worker(worker_id)
        if w is None:
            return
        self._apply_usage(w, usage)
        if cost_usd is not None:
            w.cost_usd += float(cost_usd)

    @staticmethod
    def _apply_usage(w: WorkerMetrics, usage: Dict[str, Any]) -> None:
        """Accumulate token counts from an SDK usage dict (per-query delta).

        Anthropic SDK shape (as of 2026-04):
            input_tokens, output_tokens,
            cache_read_input_tokens, cache_creation_input_tokens
        Older shapes that nest these under 'tokens' are also tolerated.
        """
        if not isinstance(usage, dict):
            return
        # Some SDK versions wrap counts under a nested key.
        src = usage.get('tokens') if isinstance(usage.get('tokens'), dict) else usage
        try:
            w.tokens_input += int(src.get('input_tokens', 0) or 0)
            w.tokens_output += int(src.get('output_tokens', 0) or 0)
            w.tokens_cache_read += int(
                src.get('cache_read_input_tokens', 0) or 0
            )
            w.tokens_cache_creation += int(
                src.get('cache_creation_input_tokens', 0) or 0
            )
        except (TypeError, ValueError):
            pass

    # --- Orchestrator events ---

    def record_orchestrator_message(self, phase: str = 'other') -> None:
        """Mark an orchestrator AssistantMessage event.

        Attributes the gap since the previous event to active or idle time,
        then updates the last-event timestamp. Call ONCE per orchestrator
        AssistantMessage, regardless of how many content blocks it contains.

        `phase` classifies what this message was doing (conventions /
        serial_write / reconcile / other); the ACTIVE portion of the gap is
        added to that phase bucket so the alignment cost Ĉ_alg is measurable.
        """
        if self.current_session is None:
            return
        s = self.current_session
        now = time.time()

        prev = s._last_orch_event_ts if s._last_orch_event_ts is not None else s.t_start
        self._attribute_gap(s, prev, now, phase=phase)

        s._last_orch_event_ts = now
        s.orchestrator_message_count += 1

    def record_orchestrator_usage(
        self,
        usage: Dict[str, Any],
        cost_usd: Optional[float] = None,
    ) -> None:
        """Update orchestrator-side SDK usage.

        Tokens in usage are per-query deltas → accumulate with +=.
        total_cost_usd is session-cumulative → overwrite with =.
        """
        if self.current_session is None or not isinstance(usage, dict):
            return
        s = self.current_session
        src = usage.get('tokens') if isinstance(usage.get('tokens'), dict) else usage
        try:
            s.orchestrator_tokens_input += int(src.get('input_tokens', 0) or 0)
            s.orchestrator_tokens_output += int(src.get('output_tokens', 0) or 0)
            s.orchestrator_tokens_cache_read += int(
                src.get('cache_read_input_tokens', 0) or 0
            )
            s.orchestrator_tokens_cache_creation += int(
                src.get('cache_creation_input_tokens', 0) or 0
            )
        except (TypeError, ValueError):
            pass
        if cost_usd is not None:
            try:
                s.orchestrator_cost_usd = float(cost_usd)
            except (TypeError, ValueError):
                pass

    def record_orchestrator_tool_call(
        self,
        tool_name: Optional[str],
        tool_input: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Increment orchestrator tool-call counters and file-touch set.

        Does NOT update timing — call record_orchestrator_message() once per
        message first, then record_orchestrator_tool_call() for each tool use
        block inside that message.
        """
        if self.current_session is None or not tool_name:
            return
        s = self.current_session
        s.orchestrator_tool_calls[tool_name] = (
            s.orchestrator_tool_calls.get(tool_name, 0) + 1
        )

        if isinstance(tool_input, dict):
            for key in ('file_path', 'notebook_path', 'path'):
                val = tool_input.get(key)
                if isinstance(val, str):
                    s.orchestrator_files_touched.add(val)
                    break

    def _get_worker(self, worker_id: str) -> Optional[WorkerMetrics]:
        if self.current_session is None:
            return None
        s = self.current_session
        # Look up the storage key for this slot's currently-active launch.
        storage_key = s._active_slot_keys.get(worker_id)
        if storage_key is None:
            return None
        return s.workers.get(storage_key)


# Singleton access
_collector: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector
