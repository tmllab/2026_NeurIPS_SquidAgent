# SquidAgent

**Parallelize wisely, coordinate efficiently.**

📄 Paper: [SquidAgent: Parallelize Wisely, Coordinate Efficiently](https://arxiv.org/abs/2610.08647)

🧩 Claude Code skill version: [`/flow` skill](https://github.com/a5507203/flow-claude/tree/skill). It brings the same per-layer parallel-vs-serial scheduling into a regular Claude Code session.

SquidAgent is a multi-agent development system built on the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python). An
orchestrator agent breaks your request into a task DAG. A deterministic scheduler
then decides, **layer by layer**, whether to run the tasks in parallel or serially.

Running agents in parallel is not free. Parallel workers spend effort
re-reading context that the orchestrator already has (*re-exploration cost*), and
their independently written outputs have to be made consistent afterwards
(*alignment cost*). SquidAgent only parallelizes a layer when the speedup
outweighs these costs. It keeps both costs low in two ways:

- **Shared context:** workers start from what the orchestrator already knows
  instead of re-exploring. In worktree mode each worker is forked from the
  orchestrator's session. In agent-tool mode, the plan and conventions are
  handed to each subagent.
- **Convention block:** before launching a parallel layer, the orchestrator
  writes a shared conventions section, which turns post-hoc reconciliation into
  a small, bounded upfront cost.

## How it works

1. **Plan.** The orchestrator calls `create_plan_branch` with a task list. For
   each task it estimates `estimated_output_tokens` (τ). For each dependency
   layer it estimates `layer_convention_tokens`, the layer's alignment cost
   Ĉ_alg.
2. **Schedule.** For every layer L_k of the DAG, the scheduler computes

   ```
   T_ser(L_k) = Σ τ_i
   T_par(L_k) = max τ_i + Ĉ_alg(L_k)
   ρ_k        = T_ser / T_par
   ```

   and runs the layer **in parallel iff ρ_k > α** (α = 1.9 by default), otherwise
   **serially**. Tasks below 1,000 tokens always run serially. The scheduler is
   pure Python and takes well under 100 ms.
3. **Execute.** Parallel layers start with a conventions block and then go to
   workers. The orchestrator writes serial tasks itself, while the workers run
   or after them, depending on dependencies. How the workers run depends on the
   execution mode (below).

### Execution modes

| | **agent-tool** (default) | **worktree** (`--worktree`) |
|---|---|---|
| Workers | Claude Code `Agent`-tool subagents, all spawned in one response | Claude Agent SDK sessions, one git worktree each |
| Context sharing | Plan and conventions passed in each subagent prompt | Each worker **forks the orchestrator session** |
| Merging | Workers write directly into the working tree | `finalize_wave` merges task branches into `flow` |
| Cost | Lower | Higher (every worker carries the orchestrator's context) |
| Small layers | Layers totalling < 3,000 tokens are written directly (bypass) | Scheduled like any other layer |

**Agent-tool mode** runs each request in two phases:
1. A planning session whose only tool is `create_plan_branch`. Here the tool
   runs the scheduler and returns the schedule without creating git branches.
2. A `claude -p` run with the schedule appended to its system prompt, which
   executes the plan with the `Agent` tool.

**Worktree mode:**
- Each parallel task gets its own branch and worktree (`launch_wave`).
- Workers are forked from the orchestrator's session.
- When a wave finishes, `finalize_wave` merges the branches, removes the
  worktrees, and updates the plan. The orchestrator then runs a quick
  cross-file consistency check.

Predicted output tokens are used instead of wall-clock time because LLMs
estimate the size of what they will write much more reliably than how long it
will take.

## Requirements

- Python ≥ 3.10
- [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI installed and
  authenticated (the Agent SDK drives it; tested with Claude Code 2.1.283)
- git

## Installation

```bash
git clone <this-repo-url> SquidAgent
cd SquidAgent
pip install -e .
```

## Usage

Run SquidAgent from the root of the project you want it to work on:

```bash
cd /path/to/your/project
squidagent                         # interactive session (agent-tool mode)
squidagent --worktree              # worktree mode with session-forked workers
squidagent --worktree --max-parallel 8   # worktree mode, up to 8 concurrent workers
squidagent --single-shot           # exit after the first request completes
```

| Option | Description |
|---|---|
| `--worktree` | Use worktree mode instead of the default agent-tool mode |
| `--model ID` | Claude model ID for the orchestrator and all workers (default: `claude-sonnet-4-6`) |
| `--max-parallel N` | Maximum number of concurrent workers in worktree mode (default: 6) |
| `--effort [low\|medium\|high\|max]` | Thinking effort level |
| `--single-shot` | Exit automatically after the first request completes |
| `--verbose`, `--debug` | More detailed output |

On first launch, SquidAgent does some setup:
- Initializes a git repository if the directory isn't one yet.
- Creates a `flow` branch from a base branch you choose.
- Offers to generate a `CLAUDE.md` for the project.

Inside the session, type your request and press Enter. Available commands:

| Command | Description |
|---|---|
| `/parallel N` | Set max parallel workers (worktree mode) |
| `/model ID` | Switch model (e.g. `claude-sonnet-4-6`) |
| `/verbose`, `/debug` | Toggle output detail |
| `/auto` | Toggle autonomous mode: a user-proxy agent answers clarifying questions (worktree mode) |
| `/init` | Generate `CLAUDE.md` |
| `/help` | Show help |
| `/exit`, `/q` | Quit |

Keys: `Esc` interrupts the current task, `Ctrl+C` quits.

At the end of each request (agent-tool) or session (worktree), SquidAgent
reports the total cost and the **deliverables** the run produced:
- **What counts:** files created since the run started (full content) and lines
  added to existing files. SquidAgent's own files are excluded.
- **Reported numbers:** words, tokens, and **deliverable tokens per $ / words
  per $**.
- **Tokenizer:** tokens are counted with tiktoken's `cl100k_base` as an
  approximation, since Claude's tokenizer is not public.

## Git layout and runtime files

In worktree mode, git is the single source of truth. Plans and task metadata
are stored as structured commit messages:

```
flow                               # base branch for SquidAgent sessions
├── plan/session-YYYYMMDD-HHMMSS   # plan + schedule metadata
└── task/001-<slug>, task/002-...  # one branch per worker task
.worktrees/worker-N/               # isolated worktree per running worker
```

| Path | Contents |
|---|---|
| `.squidagent/ORCHESTRATOR_INSTRUCTIONS.md`, `WORKER_INSTRUCTIONS.md` | Worktree-mode agent instructions, assembled from `squidagent/prompts/` |
| `.squidagent/sched_trace.json` | Scheduler decisions (per-layer ratio and strategy) |
| `.squidagent/metrics/` | Per-session metrics: wall time, tokens, cost, worker stats, and deliverables with cost efficiency. Agent-tool mode also saves the plan (`schedule.json`) and the full `claude -p` stream (`<session>_cli.jsonl`) |
| `~/.squidagent/logs/` | Session logs (in worktree mode: every tool call and result) |

## Configuration

The scheduler constants live in `squidagent/scheduler.py`
(`ParallelizabilityScheduler`):

| Constant | Default | Meaning |
|---|---|---|
| `PARALLEL_MARGIN` | 1.9 | Safety margin α; a layer runs in parallel only if ρ_k > α |
| `SERIAL_THRESHOLD_TOKENS` | 1000 | Tasks smaller than this always run serially |
| `CONVENTION_PLANNING_TOKENS_DEFAULT` | 3000 | Fallback Ĉ_alg when the plan gives no per-layer estimate |

The agent-tool bypass threshold (`AGENT_TOOL_BYPASS_THRESHOLD = 3000`) is in
`squidagent/agent_tool.py`.

To change agent behavior in worktree mode, edit the prompt templates in
`squidagent/prompts/`; they are reassembled at the start of every session. The
agent-tool planning prompt and execution instructions are in
`squidagent/agent_tool.py`.

## Project structure

```
squidagent/
├── commands/main.py   # CLI entry point (`squidagent`)
├── agent_tool.py      # agent-tool mode: SDK planning + `claude -p` execution
├── cli.py             # worktree mode: orchestrator session loop
├── scheduler.py       # per-layer token-cost scheduler
├── git_tools.py       # MCP tools: plans, tasks, worktrees, launch/finalize wave
├── sdk_workers.py     # worker management and session fork
├── parsers.py         # commit-message parsing
├── metrics.py         # session metrics
├── prompts/           # orchestrator / worker prompt templates
├── ui/, setup_ui/     # Textual interface
└── utils/             # message formatting, MCP config loading
```

## Citation

If you use SquidAgent in your research, please cite:

```bibtex
@article{lin2026squidagent,
  title   = {SquidAgent: Parallelize Wisely, Coordinate Efficiently},
  author  = {Lin, Yexiong and Ye, Shanshan and Yao, Yu and Fang, Zhen and Han, Bo and Liu, Tongliang},
  journal = {arXiv preprint arXiv:2610.08647},
  year    = {2026}
}
```

## License

SquidAgent is released under the [MIT License](LICENSE).
