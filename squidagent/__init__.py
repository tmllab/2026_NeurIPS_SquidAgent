"""SquidAgent: cost-aware parallel multi-agent development on the Claude Agent SDK."""

__version__ = "0.1.0"

# Defaults used throughout the package (CLI, orchestrator, workers)
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_PARALLEL = 6
DEFAULT_MODE = "agent-tool"  # or "worktree"
