"""Agent-tool execution mode (default).

Each request runs in two phases:

1. Plan (Claude Agent SDK): the orchestrator's only tool is create_plan_branch,
   in plan-only mode — it runs the scheduler (with the per-layer bypass) and
   returns the schedule without creating git branches.
2. Execute (`claude -p`): the schedule is appended to the system prompt.
   Parallel layers are written by Claude Code Agent-tool subagents spawned in
   one response; serial layers are written by the main agent itself.

Compared with worktree mode, subagents do not fork the orchestrator session,
so each worker carries much less context and runs are cheaper.
"""

import asyncio
import json
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import click

# Layers whose total estimated tokens is below this are written directly
# by the main agent instead of spawning subagents.
AGENT_TOOL_BYPASS_THRESHOLD = 3000
EXECUTION_TOOLS = "Bash,Read,Write,Edit,Grep,Glob,Agent"
STREAM_LINE_LIMIT = 64 * 1024 * 1024  # stream-json lines can carry whole files


def planning_prompt(request: str, session_id: str) -> str:
    """Phase-1 prompt: produce a task DAG via create_plan_branch, nothing else."""
    return (
        f"Analyze the following task and call mcp__git__create_plan_branch to create a plan.\n"
        f"You MUST call create_plan_branch with a task breakdown. Each task MUST have "
        f"'estimated_output_tokens' (e.g., 2000-word doc ~ 8000 tokens, small config ~ 500 tokens).\n"
        f"Do NOT execute any tasks. ONLY create the plan.\n\n"
        f"CRITICAL RULES for task structure:\n"
        f"1. Agent subagents share the orchestrator's context. Conventions (constants, class interfaces, "
        f"API signatures, naming, style) are communicated as text before spawning — subagents inherit "
        f"automatically. Therefore, tasks do NOT need depends_on for shared conventions.\n"
        f"2. Python import statements are NOT a dependency — if module B imports from module A, both "
        f"can be written in parallel as long as they agree on the interface. The interface agreement "
        f"is handled by conventions, not by depends_on.\n"
        f"3. A main entry point that imports all modules is NOT a dependency — it can be written in "
        f"parallel because the interfaces are defined by conventions. Only add depends_on when task B "
        f"must READ the actual file content produced by task A (e.g., an appendix that summarizes chapters).\n"
        f"4. Do NOT add verification/review tasks — the orchestrator handles that after all agents complete.\n"
        f"5. Estimate layer_convention_tokens — a PER-LAYER ALIGNMENT cost, NOT a single global "
        f"number. First group tasks into dependency layers: L0 = tasks with no unmet dependencies, "
        f"L1 = tasks depending only on L0, and so on. For each dependency layer, estimate how many "
        f"output tokens the orchestrator needs to write shared conventions before launching that "
        f"layer's parallel workers. Different layers may have different alignment costs — e.g., "
        f"independent chapters (L0) need fewer conventions than cross-referencing appendices (L1). "
        f"This directly affects the scheduler's per-layer parallel/serial decision — higher values "
        f"make serial more likely. Guidelines per layer:\n"
        f"   - Independent tasks with few shared interfaces: ~500-2000\n"
        f"   - Moderate tasks with shared naming, formatting, or API conventions: ~3000-4000\n"
        f"   - Tightly coupled tasks with extensive cross-file references: ~6000-9000\n"
        f"   - Aggregation layers, such as appendices, indexes, or consolidated reports over "
        f"prior-layer outputs: ~2300-2500 for a few small documents; use ~2400 and never below "
        f"2300. Scale up for more or longer documents.\n"
        f"   Pass this as the layer_convention_tokens argument, e.g. {{\"L0\": 2000, \"L1\": 2400}}. "
        f"REQUIRED: the scheduler uses each layer's own alignment cost in its per-layer parallel-vs-"
        f"serial decision (T_par(L_k) = max(tokens) + Ĉ_alg(L_k)). Do NOT collapse it into one "
        f"convention_planning_tokens value.\n\n"
        f"Session ID: {session_id}\n\n"
        f"Task:\n{request}"
    )


def execution_instructions(plan_data: Optional[Dict]) -> str:
    """Phase-2 instructions (appended to the system prompt of `claude -p`)."""
    schedule = (plan_data or {}).get("schedule")
    tasks = (plan_data or {}).get("tasks", [])
    if not (schedule and tasks):
        # No schedule — fallback to simple execution
        return """# Execution Instructions

You are an autonomous development agent. Execute the task efficiently.
Use the Agent tool to parallelize independent subtasks when beneficial.
Minimize text output.
"""

    task_lines = []
    for t in tasks:
        tid = t.get("id", "?")
        desc = t.get("description", "")
        files = t.get("files", [])
        deps = t.get("depends_on", [])
        tokens = t.get("estimated_output_tokens", 0)
        task_lines.append(
            f"- **{tid}**: {desc} (files: {files}, tokens: {tokens}, depends: {deps})"
        )

    # Merge serial waves into preceding parallel wave as "then write directly" steps
    raw_waves = schedule.get("schedule", [])
    merged_steps = []
    i = 0
    while i < len(raw_waves):
        wave = raw_waves[i]
        tids = wave.get("task_ids", [])
        strat = wave.get("strategy", "serial")
        if strat in ("staged_parallel", "full_parallel"):
            # Collect any immediately following serial waves
            serial_after = []
            j = i + 1
            while j < len(raw_waves) and raw_waves[j].get("strategy") == "serial":
                serial_after.extend(raw_waves[j].get("task_ids", []))
                j += 1
            step = f"- **Step {len(merged_steps)+1}**: Spawn Agent for tasks {tids} (parallel)"
            if serial_after:
                step += f"; write tasks {serial_after} directly yourself while they run (or after, depending on dependencies)"
            guidance = wave.get("conventions_prompt")
            if guidance:
                step += "\n  Scheduler conventions guidance:\n" + "\n".join(
                    f"    {line}" if line else "" for line in guidance.split("\n"))
            merged_steps.append(step)
            i = j
        else:
            merged_steps.append(
                f"- **Step {len(merged_steps)+1}**: Write tasks {tids} directly yourself (serial)"
            )
            i += 1

    return f"""# Execution Instructions

You are executing a pre-planned development task. Follow the schedule EXACTLY.

## Tasks
{chr(10).join(task_lines)}

## Execution Steps
{chr(10).join(merged_steps)}

## Execution Rules

1. **Before spawning agents**: Output conventions as text (shared decisions: naming, notation,
   CSS classes, nav markup, API interfaces, etc.). Agent subagents inherit your context
   automatically — do NOT write CONVENTIONS.md to a file, just output the conventions as text.
   - Follow the scheduler's conventions guidance listed under each parallel step above.
   - **FIRST — if this step depends on earlier steps, use the `Read` tool to open the FULL prior-layer output files you have NOT already read this session.** For grounding the conventions here, a `Grep` or notation spot-check is **NOT sufficient** — it verifies a pattern but loads almost none of the content into your context. Subagents receive your context through their Agent prompts, so `Read` each dependency file in full and put the real notation, section structure, identifiers, and theorem numbers that a subagent's task depends on into that subagent's prompt — do NOT make subagents re-read the upstream files. Skip any file you already `Read` earlier this session (don't re-read). The first step needs no reading — nothing has been generated yet. (This full-read requirement applies ONLY to grounding the conventions here; `Grep` remains fine later for the quick cross-file consistency check after the subagents finish.)
   - Include the full conventions in each Agent prompt (subagents share your context but be explicit).

2. For **parallel** tasks: spawn Agent subagents — one per task, ALL in ONE response.
   **CRITICAL**: You MUST include ALL Agent tool calls for a parallel step in a SINGLE response.
   The Agent tool runs calls from the same response concurrently.
   One Agent per response = serial execution = WRONG.

3. For **serial** tasks (write directly): use the Write tool yourself. Write them while the
   parallel subagents are running (or after, depending on dependencies); follow the conventions.

4. After all steps complete, do ONE quick cross-file consistency review: use a single Grep to spot-check notation/terminology across all output files. Fix any issues directly with Edit. Do NOT launch separate Agent calls per file — that costs more time than the review saves.

## Agent Spawning Format

To run tasks in parallel, use multiple Agent calls in ONE response:

```
Agent(prompt="Write <filename> at the repository root. <description> Follow these conventions: <full conventions>. <upstream facts this task needs, if any>", description="Write <filename>")
Agent(prompt="Write <filename> at the repository root. <description> Follow these conventions: <full conventions>. <upstream facts this task needs, if any>", description="Write <filename>")
```

## Output
Minimize text output. Save tokens for actual file content.
"""


def _claude_cli() -> str:
    if platform.system() == 'Windows':
        return shutil.which('claude.cmd') or shutil.which('claude') or 'claude'
    return shutil.which('claude') or 'claude'


class _Interrupted(Exception):
    pass


def _poll_control(control_queue: Optional[asyncio.Queue], deferred: List[dict]) -> Optional[str]:
    """Non-blocking check for stop/shutdown; other messages are deferred."""
    while control_queue is not None and not control_queue.empty():
        try:
            msg = control_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        kind = msg.get("type")
        if kind in ("stop", "shutdown"):
            if kind == "shutdown":
                deferred.append(msg)
            return kind
        deferred.append(msg)
    return None


async def plan(request: str, session_id: str, model: str, effort: Optional[str],
               max_parallel: int, bypass_threshold: int = AGENT_TOOL_BYPASS_THRESHOLD,
               control_queue: Optional[asyncio.Queue] = None,
               deferred: Optional[List[dict]] = None,
               usage_out: Optional[Dict] = None) -> Optional[Dict]:
    """Phase 1: SDK session that only calls create_plan_branch (plan-only).

    Returns the plan data. If usage_out is given, it is filled with the
    planning session's cost and token usage (also on interruption).
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from .git_tools import create_git_tools_server, set_plan_only, get_plan_only_result
    from .sdk_workers import create_worker_tools_server

    deferred = deferred if deferred is not None else []
    options_kwargs = {
        "system_prompt": {"type": "preset", "preset": "claude_code"},
        "allowed_tools": ["mcp__git__create_plan_branch"],
        "mcp_servers": {
            "git": create_git_tools_server(),
            "workers": create_worker_tools_server(),
        },
        "permission_mode": "bypassPermissions",
        "cwd": os.getcwd(),
        "model": model,
    }
    if effort:
        options_kwargs["effort"] = effort

    set_plan_only(True, max_parallel=max_parallel, bypass_threshold=bypass_threshold)
    try:
        async with ClaudeSDKClient(options=ClaudeAgentOptions(**options_kwargs)) as client:
            await client.query(planning_prompt(request, session_id))
            async for msg in client.receive_response():
                if type(msg).__name__ == "ResultMessage" and usage_out is not None:
                    usage_out.update(_sdk_usage(msg))
                for block in getattr(msg, "content", None) or []:
                    if type(block).__name__ == "ToolUseBlock":
                        click.echo(f"  [plan] tool: {block.name}")
                if _poll_control(control_queue, deferred):
                    await client.interrupt()
                    raise _Interrupted()
        return get_plan_only_result()
    finally:
        set_plan_only(False)


def _sdk_usage(result_msg) -> Dict:
    """Cost + token usage from a Claude Agent SDK ResultMessage."""
    usage = getattr(result_msg, "usage", None) or {}
    return {
        "cost_usd": getattr(result_msg, "total_cost_usd", None) or 0.0,
        "num_turns": getattr(result_msg, "num_turns", 0) or 0,
        "tokens": {
            "output": usage.get("output_tokens", 0),
            "input": usage.get("input_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "cache_creation": usage.get("cache_creation_input_tokens", 0),
        },
    }


def _describe_event(msg: dict) -> Optional[str]:
    """One-line summary of a stream-json event for the console."""
    kind = msg.get("type")
    prefix = "  [subagent]" if msg.get("parent_tool_use_id") else ""
    if kind == "assistant":
        lines = []
        for block in msg.get("message", {}).get("content", []) or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                lines.append(f"{prefix}{block['text'].strip()}")
            elif block.get("type") == "tool_use":
                inp = block.get("input", {}) or {}
                detail = inp.get("description") or inp.get("file_path") or inp.get("command") or ""
                lines.append(f"{prefix}[TOOL] {block.get('name')}: {str(detail)[:100]}")
        return "\n".join(lines) or None
    if kind == "result":
        return f"[RESULT] {msg.get('subtype')} — turns={msg.get('num_turns')}"
    return None


async def execute(request: str, instructions: str, model: str, effort: Optional[str],
                  log_path: Optional[Path] = None,
                  control_queue: Optional[asyncio.Queue] = None,
                  deferred: Optional[List[dict]] = None) -> Dict:
    """Phase 2: `claude -p` with the Agent tool; streams progress, returns usage summary."""
    deferred = deferred if deferred is not None else []
    cmd = [
        _claude_cli(),
        "-p", request,
        "--model", model,
        "--tools", EXECUTION_TOOLS,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        "--append-system-prompt", instructions,
    ]
    if effort:
        cmd.extend(["--effort", effort])

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=os.getcwd(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LINE_LIMIT,
    )
    log_fh = open(log_path, "wb") if log_path else None
    result_msg: Dict = {}
    total_turns = 0  # the main session may end several times (e.g. resumed by background subagents)
    n_agents = 0
    interrupted = None
    try:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                interrupted = _poll_control(control_queue, deferred)
                if interrupted:
                    proc.terminate()
                    break
                continue
            if not line:
                break
            if log_fh:
                log_fh.write(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "result":
                result_msg = msg  # cost / usage are session-cumulative: keep the last one
                total_turns += msg.get("num_turns", 0) or 0
            if msg.get("type") == "assistant" and not msg.get("parent_tool_use_id"):
                n_agents += sum(1 for b in msg.get("message", {}).get("content", []) or []
                                if b.get("type") == "tool_use" and b.get("name") == "Agent")
            text = _describe_event(msg)
            if text:
                click.echo(text)
            interrupted = _poll_control(control_queue, deferred)
            if interrupted:
                proc.terminate()
                break
        stderr = (await proc.stderr.read()).decode(errors="replace").strip()
        await proc.wait()
    finally:
        if log_fh:
            log_fh.close()

    usage = next(iter((result_msg.get("modelUsage") or {}).values()), {})
    summary = {
        "exit_code": proc.returncode,
        "interrupted": interrupted,
        "is_error": bool(result_msg.get("is_error")) or (proc.returncode not in (0, None) and not interrupted),
        "cost_usd": result_msg.get("total_cost_usd") or 0.0,
        "num_turns": total_turns,
        "n_subagents": n_agents,
        "tokens": {
            "output": usage.get("outputTokens", 0),
            "input": usage.get("inputTokens", 0),
            "cache_read": usage.get("cacheReadInputTokens", 0),
            "cache_creation": usage.get("cacheCreationInputTokens", 0),
        },
    }
    if summary["is_error"] and stderr:
        summary["stderr_tail"] = stderr[-500:]
    return summary


async def run_agent_tool_session(request: str, session_id: str, model: str,
                                 effort: Optional[str], max_parallel: int,
                                 control_queue: Optional[asyncio.Queue] = None,
                                 logger: Optional[object] = None,
                                 deferred: Optional[List[dict]] = None) -> Dict:
    """Run one request end-to-end (plan → execute) in the current directory."""
    deferred = deferred if deferred is not None else []
    metrics_dir = Path(".squidagent") / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    click.echo("=" * 60)
    click.echo(f"SquidAgent (agent-tool mode) — session {session_id}")
    click.echo(f"Model: {model} | Effort: {effort or 'default'} | Working dir: {os.getcwd()}")
    click.echo("=" * 60)
    if logger:
        logger.info(f"[AGENT-TOOL] request: {request}")

    from . import deliverables

    record: Dict = {"session_id": session_id, "mode": "agent-tool", "model": model, "effort": effort}
    plan_usage: Dict = {}
    deliv_snapshot = deliverables.snapshot()
    t0 = time.time()
    try:
        click.echo("\n[Phase 1] Planning + scheduling...")
        plan_data = await plan(request, session_id, model, effort, max_parallel,
                               control_queue=control_queue, deferred=deferred,
                               usage_out=plan_usage)
        t_plan = time.time() - t0
        schedule = (plan_data or {}).get("schedule") or {}
        record.update({
            "planning_time": round(t_plan, 1),
            "tasks": (plan_data or {}).get("tasks", []),
            "layer_convention_tokens": (plan_data or {}).get("layer_convention_tokens"),
            "scheduler_decisions": schedule.get("decisions", []),
            "schedule": schedule.get("schedule", []),
        })
        if schedule:
            click.echo(f"  Planned {len(record['tasks'])} tasks in {t_plan:.1f}s:")
            for d in record["scheduler_decisions"]:
                click.echo(f"    {d}")
        else:
            click.echo("  No schedule returned — falling back to direct execution")

        click.echo("\n[Phase 2] Executing with the Agent tool...")
        t1 = time.time()
        summary = await execute(request, execution_instructions(plan_data), model, effort,
                                log_path=metrics_dir / f"{session_id}_cli.jsonl",
                                control_queue=control_queue, deferred=deferred)
        record.update(summary)
        record["execution_time"] = round(time.time() - t1, 1)
    except _Interrupted:
        record["interrupted"] = "stop"
        click.echo("\n[STOP] Planning interrupted.")
    record["wall_time"] = round(time.time() - t0, 1)

    # Cost / tokens: planning session + `claude -p` execution
    exec_tokens = record.pop("tokens", None) or {}
    plan_tokens = plan_usage.get("tokens", {})
    record["planning_cost_usd"] = round(plan_usage.get("cost_usd", 0.0), 4)
    record["execution_cost_usd"] = round(record.get("cost_usd", 0.0), 4)
    record["cost_usd"] = round(record["planning_cost_usd"] + record["execution_cost_usd"], 4)
    record["planning_tokens"] = plan_tokens
    record["execution_tokens"] = exec_tokens
    record["tokens"] = {k: plan_tokens.get(k, 0) + exec_tokens.get(k, 0)
                        for k in ("output", "input", "cache_read", "cache_creation")}

    # Effective output: deliverable tokens/words and cost efficiency
    deliv = deliverables.measure(deliv_snapshot)
    eff = deliverables.efficiency(deliv, record["cost_usd"], record["wall_time"])
    record["deliverables"] = deliv
    record.update(eff)

    (metrics_dir / f"{session_id}.json").write_text(json.dumps(record, indent=2, default=str))
    click.echo("\n" + "=" * 60)
    if record.get("interrupted"):
        click.echo(f"Interrupted after {record['wall_time']}s.")
    else:
        click.echo(f"Done in {record['wall_time']}s (plan {record.get('planning_time')}s + "
                   f"execute {record.get('execution_time')}s) | "
                   f"cost ${record['cost_usd']:.2f} (plan ${record['planning_cost_usd']:.2f} + "
                   f"execute ${record['execution_cost_usd']:.2f}) | subagents {record.get('n_subagents', 0)}")
        click.echo(deliverables.summary_line(deliv, eff))
    click.echo(f"Metrics saved to: {metrics_dir / (session_id + '.json')}")
    click.echo("=" * 60)
    if logger:
        logger.info(f"[AGENT-TOOL] finished: {json.dumps({k: record.get(k) for k in ('wall_time', 'cost_usd', 'n_subagents', 'interrupted')})}")
    return record


async def run_agent_tool_loop(control_queue: asyncio.Queue, session_id: str,
                              settings: Callable[[], Tuple[str, Optional[str], int]],
                              single_shot: bool = False, logger: Optional[object] = None) -> None:
    """Process requests from the control queue, one agent-tool session per request.

    settings() returns the current (model, effort, max_parallel), so /model and
    /parallel changes apply to the next request.
    """
    n = 0
    while True:
        control = await control_queue.get()
        kind = control.get("type")
        if kind == "shutdown":
            break
        if kind != "intervention":
            continue  # nothing running to stop
        request = control.get("data", {}).get("requirement", "")
        if not request:
            continue
        n += 1
        model, effort, max_parallel = settings()
        deferred: List[dict] = []
        await run_agent_tool_session(request, session_id if n == 1 else f"{session_id}-{n}",
                                     model, effort, max_parallel,
                                     control_queue=control_queue, logger=logger,
                                     deferred=deferred)
        for msg in deferred:  # follow-ups / shutdown received while running
            await control_queue.put(msg)
        if single_shot:
            break
