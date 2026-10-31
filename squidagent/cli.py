"""SquidAgent CLI - Git-driven autonomous development system.

This module provides the command-line interface for SquidAgent,
which uses Claude agent SDK to orchestrate autonomous development
with git as the single source of truth.
"""

import asyncio
import os
import sys
from typing import Optional
from datetime import datetime

import click

# Global logging flags
_verbose_logging = False
_debug_logging = False
_session_logger = None  # Optional logger for file logging

# Track subagent identification (tool_use_id -> agent_name)
_tool_id_to_agent = {}

# SDK session ID for resume capability
_current_session_id = None

# Hook for Textual UI integration (ui package)
_message_handler = None


def safe_echo(text: str, nl: bool = True, **kwargs):
    """Safely print text handling Unicode/emoji on Windows.

    Windows console uses cp1252 by default which can't handle emojis.
    This function handles encoding errors gracefully.
    """
    try:
        # Try normal echo first
        click.echo(text, nl=nl, **kwargs)
    except UnicodeEncodeError:
        # Fallback: encode with error handling
        # Replace problematic chars with '?'
        safe_text = text.encode('ascii', errors='replace').decode('ascii')
        click.echo(safe_text, nl=nl, **kwargs)


def setup_instruction_files(debug: bool = False, max_parallel: int = 5) -> list:
    """Setup instruction files in .squidagent/ directory.

    This function:
    1. Creates .squidagent/ directory if needed
    2. Copies the worker instruction file if it doesn't exist
    3. Assembles the orchestrator prompt from the template + section snippets
    4. Commits them to flow branch if on flow and files are new

    Args:
        debug: Enable debug output
        max_parallel: Maximum number of parallel workers (substituted into the prompt)

    Returns:
        list: List of created file paths

    This should be called after flow branch is created/checked out.
    """
    import shutil
    import subprocess
    from pathlib import Path

    working_dir = os.getcwd()
    prompts_dir = os.path.join(os.path.dirname(__file__), 'prompts')

    # Create .squidagent directory
    squid_dir = os.path.join(working_dir, '.squidagent')
    os.makedirs(squid_dir, exist_ok=True)

    created_files = []

    # Worker instructions (just copy)
    worker_local = os.path.join(squid_dir, 'WORKER_INSTRUCTIONS.md')
    if not os.path.exists(worker_local):
        try:
            shutil.copy2(os.path.join(prompts_dir, 'worker.md'), worker_local)
            created_files.append(".squidagent/WORKER_INSTRUCTIONS.md")
            if debug:
                click.echo("DEBUG: Copied worker.md -> .squidagent/WORKER_INSTRUCTIONS.md")
        except Exception as e:
            if debug:
                click.echo(f"DEBUG: Failed to copy worker.md: {e}")

    # Orchestrator: assemble from template + section snippets
    # Always regenerate so max_parallel changes are picked up
    orch_local = os.path.join(squid_dir, 'ORCHESTRATOR_INSTRUCTIONS.md')
    orch_template = os.path.join(prompts_dir, 'orchestrator.md')
    try:
        with open(orch_template, 'r', encoding='utf-8') as f:
            content = f.read()

        sections = {
            '{{EXECUTION_SECTION}}': 'execution.md',
            '{{CONVENTIONS_SECTION}}': 'conventions.md',
            '{{WORKER_COMPLETION_SECTION}}': 'worker_completion.md',
        }
        for placeholder, snippet_name in sections.items():
            with open(os.path.join(prompts_dir, snippet_name), 'r', encoding='utf-8') as f:
                content = content.replace(placeholder, f.read().strip())
        content = content.replace('{{MAX_PARALLEL}}', str(max_parallel))

        with open(orch_local, 'w', encoding='utf-8') as f:
            f.write(content)

        if debug:
            click.echo("DEBUG: Assembled ORCHESTRATOR_INSTRUCTIONS.md")
    except Exception as e:
        # Fallback: just copy the template as-is
        if not os.path.exists(orch_local):
            try:
                shutil.copy2(orch_template, orch_local)
                created_files.append('.squidagent/ORCHESTRATOR_INSTRUCTIONS.md')
            except Exception:
                pass
        if debug:
            click.echo(f"DEBUG: Failed to assemble orchestrator prompt: {e}")

    # Auto-commit if files were created and we're on flow branch
    if created_files:
        try:
            # Check if git repo exists
            if not Path('.git').exists():
                if debug:
                    click.echo("DEBUG: No git repository - skipping auto-commit")
                return created_files

            # Check current branch
            result = subprocess.run(
                ['git', 'branch', '--show-current'],
                capture_output=True,
                text=True,
                timeout=5
            )
            current_branch = result.stdout.strip()

            if current_branch != 'flow':
                if debug:
                    click.echo(f"DEBUG: Not on flow branch ({current_branch}) - skipping auto-commit")
                return created_files

            # Stage files
            subprocess.run(
                ['git', 'add'] + created_files,
                check=True,
                timeout=10
            )

            # Commit
            commit_message = "Initialize SquidAgent instruction files\n\nAuto-committed by SquidAgent"
            subprocess.run(
                ['git', 'commit', '-m', commit_message],
                check=True,
                capture_output=True,
                timeout=10
            )

            if debug:
                click.echo(f"DEBUG: ✓ Committed instruction files to flow branch")

        except Exception as e:
            if debug:
                click.echo(f"DEBUG: Could not auto-commit instruction files: {e}")

    return created_files

try:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition
except ImportError:
    print("ERROR: claude-agent-sdk is not installed.", file=sys.stderr)
    print("Install it with: pip install claude-agent-sdk", file=sys.stderr)
    print("\nNote: You also need Claude Code CLI installed:", file=sys.stderr)
    print("  npm install -g @anthropic-ai/claude-code", file=sys.stderr)
    sys.exit(1)


from .git_tools import create_git_tools_server
from .sdk_workers import create_worker_tools_server
from .utils.text_formatter import format_tool_input, format_tool_result
from .utils.mcp_loader import load_project_mcp_config
from .metrics import get_metrics_collector


def check_claude_code_available() -> tuple[bool, str]:
    """Check if Claude Code CLI is installed and available.

    Returns:
        Tuple of (is_available, message)
    """
    import subprocess
    import platform

    # On Windows, try claude.cmd; on Unix, try claude
    commands_to_try = []
    if platform.system() == 'Windows':
        commands_to_try = ['claude.cmd', 'claude']
    else:
        commands_to_try = ['claude']

    # Try each command variant
    for cmd in commands_to_try:
        try:
            result = subprocess.run(
                [cmd, '--version'],
                capture_output=True,
                text=True,
                timeout=5,
                shell=True if platform.system() == 'Windows' else False
            )

            if result.returncode == 0:
                return True, f"Claude Code CLI found: {result.stdout.strip()}"

        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return False, "Claude Code CLI check timed out"
        except Exception:
            continue

    # If we get here, no command worked
    return False, ("Claude Code CLI not found. Install it with:\n"
                  "  npm install -g @anthropic-ai/claude-code")


async def run_development_session(
    initial_request: str,
    session_id: str,
    model: str,
    permission_mode: str,
    enable_parallel: bool,
    max_parallel: int,
    verbose: bool,
    debug: bool,
    num_workers: int,
    control_queue: Optional[asyncio.Queue] = None,
    logger: Optional[object] = None,  # SessionLogger instance
    auto_mode: bool = True,  # Enable user agent for autonomous decisions
    resume_session_id: Optional[str] = None,  # Resume from previous session
    single_shot: bool = False,  # Exit after first request completes
    effort: str = None,  # Thinking effort level (low/medium/high/max)
) -> bool:
    """Run persistent development session handling all follow-ups internally.

    This function runs a continuous session that processes the initial request
    and all subsequent follow-up requests from control_queue. It only returns
    when the user explicitly quits via /quit command.

    Args:
        initial_request: User's initial development request
        session_id: Session ID for logging (persistent across requests)
        model: Claude model ID (default: squidagent.DEFAULT_MODEL)
        max_turns: Maximum conversation turns per request
        permission_mode: Permission mode for tools
        enable_parallel: Enable parallel task execution
        max_parallel: Maximum number of parallel workers
        verbose: Enable verbose logging
        debug: Enable debug mode
        num_workers: Number of worker agents to create
        control_queue: Queue for receiving follow-up requests
        logger: Logger instance for session logging
        auto_mode: Enable autonomous mode with user proxy agent
        resume_session_id: Resume from previous session ID

    Returns:
        True if user requested quit, False if session ended naturally

    Note:
        All prompts use @filepath syntax.
        The SDK loads the full file content at runtime.
    """
    # Store logging flags and logger globally for use in handle_agent_message
    global _verbose_logging, _debug_logging, _session_logger, _tool_id_to_agent, _current_session_id
    _verbose_logging = verbose
    _debug_logging = debug
    _session_logger = logger
    _tool_id_to_agent = {}  # Clear agent tracking for new session
    _current_session_id = None  # Reset session ID for new session (will be captured from SystemMessage)

    # Note: SDKWorkerManager is initialized in ui/orchestrator.py before calling this function
    # The singleton pattern ensures the same instance (with proper worker_log) is used throughout
    # Initializing again here would overwrite the UI log function with click.echo, breaking output

    # Propagate session config to worker manager (used by launch_wave / workers)
    from squidagent.sdk_workers import get_sdk_worker_manager as _get_mgr
    _mgr_init = _get_mgr(max_parallel=max_parallel)
    _mgr_init._model = model
    _mgr_init._effort = effort

    # Ensure orchestrator instructions are assembled in the session's cwd
    # (setup_instruction_files in commands/main.py may have run before cwd was set)
    setup_instruction_files(debug=debug)

    # Start metrics collection for this session
    # max_parallel=1 when sequential so recommendations interpret correctly
    _metrics_max_parallel = max_parallel if enable_parallel else 1
    get_metrics_collector().start_session(session_id, _metrics_max_parallel)
    # Snapshot the repo so the session's deliverables can be measured at the end
    from . import deliverables as _deliverables
    _deliv_snapshot = _deliverables.snapshot()

    # Print banner
    click.echo("=" * 60)
    click.echo("SquidAgent Development Session Starting...")
    click.echo("=" * 60)
    click.echo(f"\nInitial Request: {initial_request}")
    click.echo(f"Session ID: {session_id}")
    click.echo(f"Model: {model}")
    click.echo(f"Working Directory: {os.getcwd()}")
    click.echo(f"Execution Mode: {'Parallel' if enable_parallel else 'Sequential'}")
    if enable_parallel:
        click.echo(f"Max Parallel Workers: {max_parallel}")

    # Show logging modes
    logging_modes = []
    if verbose:
        logging_modes.append("Verbose")
    if debug:
        logging_modes.append("Debug")
    if logging_modes:
        click.echo(f"Logging: {', '.join(logging_modes)}")

    click.echo()

    # Use passed session_id instead of generating new one
    plan_branch = f"plan/{session_id}"

    if debug:
        safe_echo(f"DEBUG: Using session ID: {session_id}")
        safe_echo(f"DEBUG: Plan branch: {plan_branch}")

    # Set git config for current plan branch (required for mcp__git__read_plan_file tool)
    import subprocess
    import platform
    import shutil

    try:
        # Set the current plan branch name that MCP tool will use
        subprocess.run(
            ["git", "config", "--local", "squidagent.current-plan", plan_branch],
            capture_output=True,
            check=True
        )
        if debug:
            safe_echo(f"DEBUG: Set git config squidagent.current-plan='{plan_branch}'")
    except subprocess.CalledProcessError as e:
        if debug:
            safe_echo(f"DEBUG: Warning: Could not set git config: {e}")

    # Find Claude CLI path

    claude_path = None
    if platform.system() == 'Windows':
        # Try shutil.which first (Python's cross-platform way)
        claude_path = shutil.which('claude.cmd') or shutil.which('claude')

        # If not found, try common Windows locations
        if not claude_path:
            common_locations = [
                os.path.join(os.environ.get('APPDATA', ''), 'npm', 'claude.cmd'),
                os.path.join(os.environ.get('USERPROFILE', ''), 'AppData', 'Roaming', 'npm', 'claude.cmd'),
                'C:\\Program Files\\nodejs\\claude.cmd',
                os.path.join(os.environ.get('ProgramFiles', ''), 'nodejs', 'claude.cmd'),
            ]

            for location in common_locations:
                if location and os.path.exists(location):
                    claude_path = location
                    break

        # If still not found, try npm config
        if not claude_path:
            try:
                npm_prefix = subprocess.run(
                    'npm config get prefix',
                    capture_output=True,
                    text=True,
                    shell=True,
                    timeout=5
                ).stdout.strip()
                if npm_prefix:
                    potential_path = os.path.join(npm_prefix, 'claude.cmd')
                    if os.path.exists(potential_path):
                        claude_path = potential_path
            except Exception:
                pass
    else:
        # On Unix, use shutil.which
        claude_path = shutil.which('claude')

    # Show Claude CLI path in debug mode
    if debug:
        if claude_path:
            click.echo(f"DEBUG: Found Claude CLI at: {claude_path}")
        else:
            click.echo("DEBUG: Claude CLI path not found, SDK will use default detection")

    # Orchestrator (orchestrator.md) plans, schedules, and spawns workers
    # User Proxy (inline prompt below) handles user confirmations (auto-mode only)
    # Workers (worker.md) execute individual tasks in parallel
    # SDK v0.1.6+ automatically handles Windows cmd.exe 8191-char limit via temp files (PR #245)
    agent_definitions = {}

    # Define user subagent - handles user confirmations
    # Only register if auto_mode is enabled
    if auto_mode:
        agent_definitions['user'] = AgentDefinition(
            description='User agent that represents the user for confirmation dialogs',
            prompt='''You help user to make decision

## Core Responsibilities

When invoked, you analyze proposals, plans, and options, then make decisions based on software engineering best practices. You provide clear technical justification and return decisions immediately without waiting for human input.

## Decision-Making Framework

### Plan Review Criteria
When evaluating implementation plans, assess:
1. **Completeness**: Does it cover all stated requirements?
2. **Feasibility**: Are time estimates and task breakdowns realistic?
3. **Technology Appropriateness**: Are chosen technologies suitable for the use case?
4. **Architecture Soundness**: Is the proposed structure logical and maintainable?

### Technology/Design Decision Criteria
When choosing between options, prioritize:
1. **Requirements Fit**: Which option best serves the stated needs?
2. **Simplicity**: When equivalent, prefer simpler over complex solutions
3. **Standard Practice**: Favor widely-adopted patterns and technologies
4. **Maintainability**: Choose options that are easier to understand and modify


## Operational Principles

### DO:
- Carefully read and understand all context before deciding
- Apply established software engineering best practices
- Choose simplicity for straightforward requirements
- Provide specific, actionable technical reasoning
- Make decisions quickly and confidently
- Trust your technical judgment

## Your Mission

You are a proxy for an experienced software engineer making real-time technical decisions. The orchestrator and other agents trust you to catch flawed plans, make smart architectural choices, resolve ambiguities sensibly, and keep projects moving efficiently.

Every decision you make should reflect what a skilled engineer would choose when reviewing proposals during active development. Be thoughtful, be decisive, and always explain your technical reasoning clearly.

**Trust your judgment. Analyze. Decide. Justify. Execute.**
''',
            tools=[
                'Bash', 'Read', 'Edit', 'Grep', 'Glob',
                'mcp__git__parse_task',  # Read task metadata from branch
                'mcp__git__parse_plan',  # Read plan context
                'mcp__git__parse_worker_commit',  # Read own progress (commit-only architecture)
                'mcp__git__get_provides'  # Query available capabilities from completed tasks
            ],  # User proxy doesn't need tools - just facilitates user interaction
            model=model,
            effort=effort,
        )


    if debug:
        user_count = 1 if auto_mode else 0
        click.echo(f"DEBUG: Created {len(agent_definitions)} agent definitions ({user_count} user proxy + {num_workers} workers):")
        for agent_name, agent_def in agent_definitions.items():
            click.echo(f"DEBUG:   - {agent_name}: {agent_def.description}")
            click.echo(f"DEBUG:     tools: {agent_def.tools}")
            click.echo(f"DEBUG:     model: {agent_def.model}")
            click.echo(f"DEBUG:     prompt length: {len(agent_def.prompt)} chars")
        click.echo()


    orchestrator_prompt = {
        "type": "preset",
        "preset": "claude_code",
    }

    # Orchestrator tools: core + git MCP tools + worktree/worker execution tools
    allowed_tools = [
        "Bash", "Read", "Write", "Edit", "Grep", "Glob",
        # MCP git tools (always available)
        "mcp__git__parse_task",
        "mcp__git__parse_plan",
        "mcp__git__get_provides",
        "mcp__git__parse_worker_commit",
        "mcp__git__create_plan_branch",
        "mcp__git__create_task_branch",
        "mcp__git__update_plan_branch",
        # Worktree execution: SDK workers in git worktrees
        "mcp__git__create_worktree",
        "mcp__git__remove_worktree",
        "mcp__git__launch_wave",
        "mcp__git__finalize_wave",
        "mcp__workers__launch_worker_async",
        "mcp__workers__get_worker_status",
    ]

    # Configure agent options with programmatic agents
    options_kwargs = {
        "system_prompt": orchestrator_prompt,
        "agents": agent_definitions,
        "allowed_tools": allowed_tools,
        "mcp_servers": {
            # SDK MCP servers (always available)
            "git": create_git_tools_server(),
            "workers": create_worker_tools_server(),
            # Load additional MCP servers from project's .mcp.json
            **load_project_mcp_config()
        },
        "permission_mode": permission_mode,
        "cwd": os.getcwd(),
        "cli_path": claude_path,
        "model": model,
        "setting_sources":["user", "project", "local"]  # Explicitly set Claude CLI path
    }

    if effort:
        options_kwargs["effort"] = effort

    # Add resume parameter if provided (session resumption)
    if resume_session_id:
        options_kwargs["resume"] = resume_session_id
        if debug:
            click.echo(f"DEBUG: Resuming session: {resume_session_id}")

    if debug:
        _debug_msg = f"DEBUG: options_kwargs: {options_kwargs}"
        click.echo(_debug_msg)
        if logger:
            logger.debug(_debug_msg)

    options = ClaudeAgentOptions(**options_kwargs)

    if debug:
        click.echo(f"DEBUG: ClaudeAgentOptions created")
        click.echo(f"DEBUG: system_prompt: {orchestrator_prompt}")
        if options.agents:
            click.echo(f"DEBUG: Agent names in options: {list(options.agents.keys())}")
        else:
            click.echo(f"DEBUG: WARNING: options.agents is None or empty!")
        click.echo()

    # Create session configuration for orchestrator
    # The orchestrator-minimal.md system prompt is loaded via @filepath in ClaudeAgentOptions
    # This initial query provides session-specific configuration
    parallel_config = ""
    if enable_parallel:
        parallel_config = f"""
**Parallel Execution:** ENABLED
- Max parallel workers: {max_parallel}
- Workers available: {', '.join([f'worker-{i}' for i in range(1, num_workers + 1)])}
- Execute tasks immediately when dependencies are met
- Acknowledge each worker completion briefly; merge the whole wave with `mcp__git__finalize_wave` once all its workers are done
- Respect max parallel limit
"""
    else:
        parallel_config = f"""
**Parallel Execution:** DISABLED
- Worker available: worker-1
- Execute tasks sequentially (one at a time)
"""

    # Quick-start steps shown in every request prompt
    _steps = ["Analyze request → plan tasks with `mcp__git__create_plan_branch` (it returns the schedule)"]
    if auto_mode:
        _steps.append("Call user agent for confirmation")
    _steps += [
        "Parallel waves: output conventions, then launch the whole wave with `mcp__git__launch_wave`; write the serial tasks yourself while the workers run (or after, depending on dependencies)",
        "After launching a wave and writing the serial tasks that can be written now, end your turn — you will be notified as each worker finishes (do not poll worker status)",
        "When notified of a worker completion: acknowledge briefly; do NOT merge per worker",
        "When the whole wave is complete: call `mcp__git__finalize_wave` once (merges all task branches, removes worktrees, updates the plan), then write any serial tasks that were waiting on it and launch the next wave",
        "Report when all tasks are done",
    ]
    quick_start = "\n".join(f"{i}. {step}" for i, step in enumerate(_steps, 1))

    # Helper function to create query prompt for a request
    def create_query_prompt(user_request: str) -> str:
        user_agent_guide = ""
        if auto_mode:
            user_agent_guide = """


**IMPORTANT: DO NOT ask questions directly - always invoke user subagent. **
"""

        return f"""# Development Session Configuration 

**User Request:** {user_request}

**Session Info:**
- Session ID: {session_id}
- Plan Branch: {plan_branch}
- Working Directory: {os.getcwd()}
- Max Workers: {max_parallel}

**Instructions:** See .squidagent/ORCHESTRATOR_INSTRUCTIONS.md for your full workflow.

{parallel_config}

{f'**Available Subagents:**{chr(10)}- **user** - Get confirmations/decisions (call with Task tool){chr(10)}{chr(10)}' if auto_mode else ''}**Workers:** {', '.join(f'worker-{i}' for i in range(1, num_workers + 1))} are launched via `mcp__git__launch_wave` (not the Task tool)
{user_agent_guide}
---

**Quick Start:**
{quick_start}

Begin."""

    # Persistent session - processes ALL requests in a continuous loop
    # Only exits when user sends shutdown signal
    try:
        if not control_queue:
            click.echo("ERROR: control_queue is required", err=True)
            return

        # Auto-resume configuration for crash recovery
        retry_count = 0
        max_retries = 100

        while retry_count < max_retries:
            try:
                # Add resume parameter if we have session_id from previous crash
                if _current_session_id and retry_count > 0:
                    options_kwargs["resume"] = _current_session_id
                    if debug:
                        click.echo(f"DEBUG: Resuming with session_id: {_current_session_id}")

                # Create SDK client ONCE - session persists across all requests
                options = ClaudeAgentOptions(**options_kwargs)

                if debug:
                    click.echo(f"DEBUG: Creating persistent ClaudeSDKClient session...")

                async with ClaudeSDKClient(options=options) as client:
                    if debug:
                        click.echo(f"DEBUG: SDK client session started")

                    # Continuous loop - processes requests until shutdown
                    while True:
                        if debug:
                            click.echo(f"\nDEBUG: Waiting for next request from control_queue...")

                        # Wait for next request (blocks here)
                        control = await control_queue.get()

                        # Handle shutdown
                        if control.get("type") == "shutdown":
                            click.echo("\n[SHUTDOWN] User requested exit\n")
                            break

                        # Handle worker completion events from async workers
                        if control.get("type") == "worker_completion":
                            worker_data = control.get("data", {})
                            worker_id = worker_data.get("worker_id")
                            task_branch = worker_data.get("task_branch")
                            exit_code = worker_data.get("exit_code")
                            elapsed_time = worker_data.get("elapsed_time", 0)
                            error_message = worker_data.get("error_message")  # May be None

                            # Format elapsed time
                            elapsed_min = int(elapsed_time / 60)
                            elapsed_sec = int(elapsed_time % 60)

                            # Log the completion event to console
                            status_label = '(success)' if exit_code == 0 else '(stopped)' if exit_code == 2 else '(failed)'
                            click.echo("\n" + "=" * 60)
                            click.echo(f"[WORKER COMPLETION EVENT]")
                            click.echo(f"  Worker ID: {worker_id}")
                            click.echo(f"  Task: {task_branch}")
                            click.echo(f"  Exit code: {exit_code} {status_label}")
                            click.echo(f"  Duration: {elapsed_min}m {elapsed_sec}s")
                            if error_message:
                                click.echo(f"  Message: {error_message}")
                            click.echo("=" * 60 + "\n")

                            # Also log to file for tracking
                            if logger:
                                logger.info(f"[WORKER COMPLETION] Worker-{worker_id} completed {task_branch}")
                                logger.info(f"  Exit code: {exit_code} {status_label}, Duration: {elapsed_min}m {elapsed_sec}s")
                                if error_message:
                                    if exit_code == 2:
                                        logger.warning(f"  Message: {error_message}")
                                    else:
                                        logger.error(f"  Error: {error_message}")

                            if debug:
                                click.echo(f"DEBUG: Worker completion event details:")
                                click.echo(f"DEBUG:   Worker-{worker_id}")
                                click.echo(f"DEBUG:   Branch: {task_branch}")
                                click.echo(f"DEBUG:   Exit code: {exit_code}")
                                click.echo(f"DEBUG:   Elapsed: {elapsed_time:.2f}s")
                                click.echo(f"DEBUG: Injecting completion notification to orchestrator\n")
                                if logger:
                                    logger.debug(f"Worker-{worker_id} completion: branch={task_branch}, exit={exit_code}, elapsed={elapsed_time:.2f}s")

                            # Record completion in scheduler and build wave-aware guidance
                            from squidagent.sdk_workers import get_sdk_worker_manager
                            _mgr = get_sdk_worker_manager()
                            _sched_info = _mgr.record_task_completion(task_branch) if _mgr else {}
                            _wave_guidance = ""
                            if _sched_info.get("has_schedule"):
                                if _sched_info.get("wave_advanced"):
                                    _next_tasks = _sched_info.get("next_wave_tasks", [])
                                    _next_strat = _sched_info.get("next_wave_strategy", "")
                                    _wave_guidance = f"""

        SCHEDULE UPDATE: Wave {_sched_info['new_wave_idx'] - 1} complete. Advancing to wave {_sched_info['new_wave_idx']}.
        Next wave tasks: {_next_tasks} (strategy: {_next_strat})
        Call `mcp__git__finalize_wave` for the completed wave now (use its `next_wave` to launch these tasks)."""
                                elif _sched_info.get("remaining_in_wave"):
                                    _remaining = _sched_info["remaining_in_wave"]
                                    _wave_guidance = f"""

        SCHEDULE: Still in wave {_sched_info['current_wave_idx']}. Remaining tasks in this wave: {_remaining}
        Wait for these to complete before advancing to the next wave."""
                                elif _sched_info.get("schedule_complete"):
                                    _wave_guidance = """

        SCHEDULE: All scheduled waves complete. Session is done."""

                            # Create notification message for orchestrator
                            if exit_code == 0:
                                # Minimal per-worker notification. The orchestrator just
                                # acknowledges; batch processing via finalize_wave at wave end.
                                completion_msg = f"""Worker-{worker_id} completed task {task_branch} (success, {elapsed_min}m {elapsed_sec}s).
        Acknowledged — do NOT merge/cleanup/update yet.{_wave_guidance}"""
                            elif exit_code == 2:
                                # Stopped case - worker was cancelled by orchestrator
                                error_details = f"\n- Reason: {error_message}" if error_message else ""
                                completion_msg = f"""Worker-{worker_id} was STOPPED for task {task_branch}
        - Exit code: {exit_code} (stopped)
        - Elapsed time: {elapsed_min}m {elapsed_sec}s{error_details}

        The worker was intentionally stopped by you before completing the task. Please:
        1. Remove the stopped worktree: mcp__git__remove_worktree("{worker_id}")
        2. Decide on next steps for the incomplete task:
           - If task should be completed: Launch a new worker for the same task branch
           - If task is no longer needed: Update plan to mark as cancelled/skipped
           - If changing approach: Create a new task branch with different strategy
        3. Update plan if needed: mcp__git__update_plan_branch()
        4. Worker-{worker_id} is now idle and available for new tasks
        5. Continue with other workers' tasks independently"""
                            else:
                                # Error case - include error details and suggest retry/alternative
                                error_details = f"\n- Error: {error_message}" if error_message else ""
                                completion_msg = f"""Worker-{worker_id} FAILED task {task_branch}
        - Exit code: {exit_code} (error)
        - Elapsed time: {elapsed_min}m {elapsed_sec}s{error_details}

        The worker encountered an error and could not complete the task. Please:
        1. Read the error message above to understand what went wrong
        2. Remove the failed worktree: mcp__git__remove_worktree("{worker_id}")
        3. Decide on next steps:
           - If validation error (e.g., missing branch, bad parameters): Fix the issue and retry
           - If initialization error (e.g., git/MCP setup): Check worktree setup, then retry
           - If runtime error: Review task complexity, consider breaking into smaller tasks
           - If repeated failures: Try alternative approach or skip for now
        4. Update plan if needed: mcp__git__update_plan_branch()
        5. If retrying or launching alternative task, use worker-{worker_id} (now idle)
        6. Continue with other workers' tasks independently"""

                            # Process as a regular query
                            await client.query(completion_msg)

                            # Process the response
                            async for msg in client.receive_response():
                                handle_agent_message(msg)

                                # Check for interruptions during response
                                if control_queue and not control_queue.empty():
                                    try:
                                        peek_control = control_queue.get_nowait()

                                        if peek_control.get("type") == "stop":
                                            click.echo("\n[STOP] Interrupting worker completion handling...\n")
                                            await client.interrupt()
                                            break
                                        else:
                                            # Put back for next iteration
                                            await control_queue.put(peek_control)
                                    except asyncio.QueueEmpty:
                                        pass

                            click.echo("\n" + "=" * 60)
                            click.echo("Worker completion processed. Waiting for next event...")
                            click.echo("=" * 60)

                            # In single-shot mode, exit once all workers have finished
                            if single_shot:
                                from squidagent.sdk_workers import get_sdk_worker_manager
                                mgr = get_sdk_worker_manager()
                                active = mgr.get_active_workers() if mgr else []
                                if not active and control_queue.empty():
                                    click.echo("\n--single-shot: all workers done, exiting.")
                                    break

                            continue

                        # Handle stop_worker result events
                        elif control.get("type") == "stop_worker_result":
                            result_data = control.get("data", {})
                            success = result_data.get("success", False)
                            worker_id = result_data.get("worker_id", "unknown")

                            # Log the stop result to console
                            click.echo("\n" + "=" * 60)
                            click.echo(f"[STOP WORKER RESULT]")
                            click.echo(f"  Worker ID: {worker_id}")

                            if success:
                                task_branch = result_data.get("task_branch", "unknown")
                                elapsed = result_data.get("elapsed_seconds", 0)
                                message = result_data.get("message", "Worker stopped")

                                click.echo(f"  Status: SUCCESS")
                                click.echo(f"  Task: {task_branch}")
                                click.echo(f"  Elapsed: {elapsed}s")
                                click.echo(f"  Message: {message}")

                                # Log to file
                                if logger:
                                    logger.info(f"[STOP WORKER] Worker-{worker_id} stopped successfully")
                                    logger.info(f"  Task: {task_branch}, Elapsed: {elapsed}s")

                                # Notify orchestrator
                                notification_msg = f"""Worker-{worker_id} has been stopped successfully.

        Task: {task_branch}
        Elapsed time: {elapsed}s

        The worker was cancelled and cleaned up. A completion event (exit_code=2) has been sent.
        You can now assign a new task to worker-{worker_id} if needed."""

                                await client.query(notification_msg)

                                # Process the response
                                async for msg in client.receive_response():
                                    handle_agent_message(msg)

                            else:
                                error = result_data.get("error", "Unknown error")
                                active_workers = result_data.get("active_workers", [])

                                click.echo(f"  Status: FAILED")
                                click.echo(f"  Error: {error}")
                                if active_workers:
                                    click.echo(f"  Active workers: {active_workers}")

                                # Log to file
                                if logger:
                                    logger.warning(f"[STOP WORKER] Failed to stop worker-{worker_id}")
                                    logger.warning(f"  Error: {error}")

                                # Notify orchestrator of failure
                                notification_msg = f"""Failed to stop worker-{worker_id}.

        Error: {error}
        Active workers: {active_workers if active_workers else 'none'}

        The worker could not be stopped. It may have already completed or was never running."""

                                await client.query(notification_msg)

                                # Process the response
                                async for msg in client.receive_response():
                                    handle_agent_message(msg)

                            click.echo("=" * 60)
                            continue

                        # Handle intervention (normal request)
                        elif control.get("type") == "intervention":
                            user_request = control.get("data", {}).get("requirement", "")
                            if not user_request:
                                click.echo("WARNING: Empty request received", err=True)
                                continue

                            click.echo(f"\n[REQUEST] Processing: {user_request}\n")
                            if debug:
                                click.echo(f"DEBUG: Sending query to SDK...")

                            # Send query
                            prompt = create_query_prompt(user_request)
                            await client.query(prompt)

                            # Process response with interruption support
                            interrupted = False
                            async for msg in client.receive_response():
                                handle_agent_message(msg)

                                # Check for interruptions during response processing
                                if control_queue and not control_queue.empty():
                                    try:
                                        peek_control = control_queue.get_nowait()

                                        if peek_control.get("type") == "stop":
                                            # User requested stop - interrupt current task
                                            click.echo("\n[STOP] Interrupting current task...\n")
                                            await client.interrupt()
                                            interrupted = True

                                        elif peek_control.get("type") == "shutdown":
                                            # Shutdown request - put back and break
                                            await control_queue.put(peek_control)
                                            break

                                        else:
                                            # Put back any other event (worker_completion,
                                            # intervention, etc.) for the main loop to handle
                                            await control_queue.put(peek_control)

                                    except asyncio.QueueEmpty:
                                        pass

                            if interrupted:
                                click.echo("[STOP] Task interrupted. Waiting for next request...")
                            else:
                                click.echo("\n" + "=" * 60)
                                click.echo("Request complete. Waiting for next request...")
                                click.echo("=" * 60)

                                # In single-shot mode, exit if no workers are active
                                # (handles the case where orchestrator executes directly
                                # without launching any workers, e.g. scheduler said SERIAL)
                                if single_shot:
                                    from squidagent.sdk_workers import get_sdk_worker_manager
                                    import asyncio as _aio
                                    mgr = get_sdk_worker_manager()
                                    active = mgr.get_active_workers() if mgr else []
                                    if not active and control_queue.empty():
                                        # Brief wait for any in-flight completion events
                                        await _aio.sleep(1.0)
                                        active = mgr.get_active_workers() if mgr else []
                                        if not active and control_queue.empty():
                                            click.echo("\n--single-shot: no active workers, exiting.")
                                            break

                            # Loop back to wait for next request (same SDK session!)
                            continue

                        # Unknown control type
                        else:
                            click.echo(f"WARNING: Unknown control type: {control.get('type')}", err=True)
                            continue

                    # Session ended normally
                    click.echo("\nSDK session ended")
                    break  # Exit retry loop on normal completion

            except Exception as e:
                retry_count += 1
                if _current_session_id and retry_count < max_retries:
                    click.echo(f"\n[CRASH] Session crashed: {e}")
                    click.echo(f"[RESUME] Auto-resuming session {_current_session_id} (attempt {retry_count}/{max_retries})...")
                    await asyncio.sleep(2)  # Brief delay before resume
                    continue
                else:
                    # No session ID or max retries reached
                    click.echo(f"\nERROR: Session failed after {retry_count} attempts", err=True)
                    raise

    except Exception as e:
        click.echo(f"\nERROR: Error during development session: {e}", err=True)
        raise
    finally:
        # Finalize metrics and emit report
        try:
            collector = get_metrics_collector()
            finished = collector.end_session()
            if finished is not None:
                try:
                    finished.deliverables = _deliverables.measure(_deliv_snapshot)
                except Exception:
                    pass
                if finished.workers:
                    click.echo("")
                    click.echo(finished.format_report())
                try:
                    fpath = finished.save_json()
                    click.echo(f"\nMetrics saved to: {fpath}")
                except Exception as save_err:
                    if debug:
                        click.echo(f"DEBUG: Failed to save metrics JSON: {save_err}")
        except Exception as metrics_err:
            # Metrics must never break the session
            if debug:
                click.echo(f"DEBUG: Metrics finalization error: {metrics_err}")


def _classify_orchestrator_phase(content) -> str:
    """Classify an orchestrator AssistantMessage into an alignment-cost phase.

    Buckets (the empirical Ĉ_alg the scheduler models):
      conventions  — writing the ## Conventions block (fork) or CONVENTIONS.md (file)
      serial_write — writing a SERIAL-decided task's output directly (Write)
      reconcile    — post-merge cross-file consistency pass (Grep / Edit)
      other        — planning, launch/finalize overhead, reads, thinking
    Best-effort from block types; Write wins over Edit/Grep so creating a
    serial file isn't miscounted as reconciliation.
    """
    if not isinstance(content, list):
        return 'other'
    conv = write = fixy = False
    for b in content:
        bt = type(b).__name__
        if bt == 'TextBlock':
            if '## Conventions' in (getattr(b, 'text', '') or ''):
                conv = True
        elif bt == 'ToolUseBlock':
            name = getattr(b, 'name', '') or ''
            inp = getattr(b, 'input', {}) or {}
            path = str(inp.get('file_path') or inp.get('path') or '') if isinstance(inp, dict) else ''
            if 'CONVENTION' in path.upper():
                conv = True
            elif name == 'Write':
                write = True
            elif name in ('Edit', 'MultiEdit', 'NotebookEdit', 'Grep'):
                fixy = True
    if conv:
        return 'conventions'
    if write:
        return 'serial_write'
    if fixy:
        return 'reconcile'
    return 'other'


def handle_agent_message(msg):
    """Handle and display agent messages with enhanced logging.

    Uses unified message formatter from squidagent.utils.message_formatter
    for consistent formatting across orchestrator and worker messages.

    Args:
        msg: Message object from SDK (SystemMessage, AssistantMessage, etc.) or dict

    Message types:
        - SystemMessage: System messages
        - AssistantMessage: Agent text output (contains TextBlock and ToolUseBlock)
        - ResultMessage: Tool results
        - UserMessage: User input
        - dict with "type": Legacy format
    """
    global _verbose_logging, _debug_logging, _session_logger, _tool_id_to_agent, _current_session_id, _message_handler

    timestamp = datetime.now().strftime("%H:%M:%S")

    # Handle SDK message objects
    from claude_agent_sdk import SystemMessage, AssistantMessage, ResultMessage, UserMessage

    # Determine agent name from parent_tool_use_id
    agent_name = "orchestrator"  # Default to orchestrator
    if isinstance(msg, AssistantMessage):
        parent_id = getattr(msg, 'parent_tool_use_id', None)
        if parent_id:
            agent_name = _tool_id_to_agent.get(parent_id, f"agent-{parent_id[:8]}")

    if isinstance(msg, SystemMessage):
        # Capture session ID from init message
        if hasattr(msg, 'subtype') and msg.subtype == 'init':
            if hasattr(msg, 'data') and isinstance(msg.data, dict) and 'session_id' in msg.data:
                _current_session_id = msg.data['session_id']
                if _debug_logging:
                    click.echo(f"[{timestamp}] [SESSION] Captured session ID: {_current_session_id}")
                # Propagate to worker manager for session fork support
                try:
                    from squidagent.sdk_workers import get_sdk_worker_manager
                    manager = get_sdk_worker_manager()
                    manager.orchestrator_session_id = _current_session_id
                except Exception:
                    pass  # Worker manager may not be initialized yet

        # System message - show in debug mode
        if _debug_logging:
            click.echo(f"[{timestamp}] [SYSTEM] {msg.content if hasattr(msg, 'content') else str(msg)}")
            import sys
            sys.stdout.flush()
        return

    elif isinstance(msg, AssistantMessage):
        # Assistant message contains content blocks (TextBlock, ToolUseBlock)
        content = msg.content if hasattr(msg, 'content') else str(msg)

        # Metrics: record one orchestrator event per AssistantMessage from the
        # orchestrator itself (subagent messages already get tracked elsewhere).
        # Must run BEFORE block iteration so tool-call recording sees the fresh
        # timestamp from record_orchestrator_message().
        if agent_name == "orchestrator":
            try:
                _phase = _classify_orchestrator_phase(content)
                get_metrics_collector().record_orchestrator_message(_phase)
            except Exception:
                pass  # Metrics must never break the session

        # Content can be a string or a list of content blocks
        if isinstance(content, str):
            # Simple string content
            if content:
                if _debug_logging:
                    safe_echo(f"[{timestamp}] {content}")
                else:
                    safe_echo(content)
                import sys
                sys.stdout.flush()
        elif isinstance(content, list):
            # List of content blocks (TextBlock, ToolUseBlock, etc.)
            for block in content:
                block_type = type(block).__name__

                if block_type == 'TextBlock':
                    # Text output from agent
                    text = block.text if hasattr(block, 'text') else str(block)
                    if text:
                        # Log to file if logger available
                        if _session_logger:
                            _session_logger.info(f"[{agent_name.upper()}] {text}")

                        # Send to Textual UI if available, otherwise print to terminal
                        if _message_handler:
                            # Textual UI mode - delegate to message handler
                            _message_handler.write_message(
                                message=text,
                                agent=agent_name,
                                timestamp=None  # Never show timestamps in UI
                            )
                        else:
                            # Terminal mode - print with agent identification
                            if _debug_logging:
                                safe_echo(f"[{timestamp}] [{agent_name.upper()}] {text}")
                            else:
                                # Show agent name for subagents, hide for orchestrator to reduce noise
                                if agent_name != "orchestrator":
                                    safe_echo(f"[{agent_name.upper()}] {text}")
                                else:
                                    safe_echo(text)
                            import sys
                            sys.stdout.flush()

                elif block_type == 'ToolUseBlock':
                    # Tool call from agent
                    tool_name = block.name if hasattr(block, 'name') else 'unknown'
                    tool_id = block.id if hasattr(block, 'id') else ''
                    tool_input = block.input if hasattr(block, 'input') else {}

                    # Track Task tool invocations to identify subagents
                    if tool_name == 'Task' and tool_id:
                        subagent_type = tool_input.get('subagent_type', 'unknown')
                        _tool_id_to_agent[tool_id] = subagent_type

                    # Metrics: record this tool call if it came from the orchestrator
                    if agent_name == "orchestrator":
                        try:
                            get_metrics_collector().record_orchestrator_tool_call(
                                tool_name,
                                tool_input,
                            )
                        except Exception:
                            pass  # Metrics must never break the session

                    # Extract key parameter for display
                    tool_detail = None
                    if tool_name == 'Read':
                        tool_detail = tool_input.get('file_path', '')
                    elif tool_name == 'Write':
                        tool_detail = tool_input.get('file_path', '')
                    elif tool_name == 'Edit':
                        tool_detail = tool_input.get('file_path', '')
                    elif tool_name == 'Bash':
                        command = tool_input.get('command', '')
                        # Truncate long commands
                        tool_detail = command[:80] + '...' if len(command) > 80 else command
                    elif tool_name == 'Grep':
                        pattern = tool_input.get('pattern', '')
                        tool_detail = f"pattern: {pattern}"
                    elif tool_name == 'Glob':
                        pattern = tool_input.get('pattern', '')
                        tool_detail = f"pattern: {pattern}"
                    elif tool_name == 'Task':
                        subagent = tool_input.get('subagent_type', 'unknown')
                        tool_detail = f"-> {subagent}"

                    # Log to file if logger available (with details + full args)
                    if _session_logger:
                        if tool_detail:
                            _session_logger.debug(f"[{agent_name.upper()}] TOOL: {tool_name} | {tool_detail}")
                        else:
                            _session_logger.debug(f"[{agent_name.upper()}] TOOL: {tool_name}")
                        if tool_input:
                            import json as _json
                            try:
                                args_str = _json.dumps(tool_input, indent=2, default=str, ensure_ascii=False)
                                _session_logger.debug(f"[{agent_name.upper()}] TOOL_ARGS: {tool_name} | {args_str}")
                            except Exception:
                                _session_logger.debug(f"[{agent_name.upper()}] TOOL_ARGS: {tool_name} | {str(tool_input)[:2000]}")

                    # Build tool message
                    tool_msg = f"[TOOL] {tool_name}"
                    if tool_detail and tool_name != 'Task':
                        tool_msg += f" | {tool_detail}"
                    # Never show tool ID in UI (only in file logs)

                    # Send to Textual UI if available, otherwise print to terminal
                    if _message_handler:
                        # Textual UI mode - delegate to message handler
                        _message_handler.write_message(
                            message=tool_msg,
                            agent=agent_name,
                            timestamp=None  # Never show timestamps in UI
                        )

                        # Show subagent invocation for Task tool
                        if tool_name == 'Task':
                            subagent_type = tool_input.get('subagent_type', 'unknown')
                            description = tool_input.get('description', '')
                            _message_handler.write_message(
                                message=f"  -> Invoking {subagent_type}: {description}",
                                agent=agent_name,
                                timestamp=None
                            )
                    else:
                        # Terminal mode - print with agent identification
                        click.echo(f"[{timestamp}] [{agent_name.upper()}] {tool_msg}")
                        import sys
                        sys.stdout.flush()

                        # Show subagent invocation for Task tool
                        if tool_name == 'Task':
                            subagent_type = tool_input.get('subagent_type', 'unknown')
                            description = tool_input.get('description', '')
                            click.echo(f"  -> Invoking {subagent_type}: {description}")
                            sys.stdout.flush()

                elif _debug_logging:
                    # Unknown block type
                    click.echo(f"[{timestamp}] [BLOCK:{block_type}] {str(block)[:200]}")
        else:
            # Unknown content type
            if _debug_logging:
                click.echo(f"[{timestamp}] [CONTENT] {str(content)[:500]}")

        return

    elif isinstance(msg, ResultMessage):
        # Metrics: capture orchestrator-side SDK token usage. Only count
        # ResultMessages that come from the orchestrator (no parent tool id),
        # since worker-side ResultMessages are already handled in
        # sdk_workers.py against the worker's storage key.
        if agent_name == "orchestrator":
            _usage = getattr(msg, 'usage', None)
            _cost = getattr(msg, 'total_cost_usd', None)
            if _usage is not None or _cost is not None:
                try:
                    get_metrics_collector().record_orchestrator_usage(
                        _usage or {},
                        _cost,
                    )
                except Exception:
                    pass  # metrics must never break the session

        # Log tool result to file if logger available
        # ResultMessage has 'result' (str), not 'content'
        if _session_logger:
            try:
                result_text = getattr(msg, 'result', None)
                if result_text:
                    if len(result_text) > 5000:
                        result_text = result_text[:5000] + f"\n... [truncated]"
                    _session_logger.debug(f"[{agent_name.upper()}] TOOL_RESULT: {result_text}")
            except Exception:
                pass

        # Tool result - show in verbose mode - FULL, NO TRUNCATION
        if _verbose_logging or _debug_logging:
            result_msg = "[RESULT] Tool execution completed"
            
            # Send to Textual UI if available, otherwise print to terminal
            if _message_handler:
                # Textual UI mode - delegate to message handler
                _message_handler.write_message(
                    message=result_msg,
                    agent=agent_name,
                    timestamp=None  # Never show timestamps in UI
                )
                
                # Show result output in debug mode
                if _debug_logging and hasattr(msg, 'content'):
                    # Format result as readable text
                    formatted_result = format_tool_result(msg.content, is_error=False)
                    # Extract just the content part (skip the "Result: SUCCESS" line)
                    result_lines = formatted_result.split('\n')[1:]  # Skip first line
                    _message_handler.write_message(
                        message='\n'.join(result_lines),
                        agent=agent_name,
                        timestamp=None
                    )
            else:
                # Terminal mode - print to console
                click.echo(f"[{timestamp}] {result_msg}")
                import sys
                sys.stdout.flush()
                
                if _debug_logging and hasattr(msg, 'content'):
                    # Format result as readable text
                    formatted_result = format_tool_result(msg.content, is_error=False)
                    # Extract just the content part (skip the "Result: SUCCESS" line)
                    result_lines = formatted_result.split('\n')[1:]  # Skip first line
                    for line in result_lines:
                        click.echo(f"  {line}")
                    sys.stdout.flush()
        return

    elif isinstance(msg, UserMessage):
        # Log tool results from UserMessage to file
        if _session_logger:
            try:
                content = getattr(msg, 'content', None)
                if isinstance(content, list):
                    for block in content:
                        block_type = type(block).__name__
                        if block_type == 'ToolResultBlock' or (isinstance(block, dict) and block.get('type') == 'tool_result'):
                            text = None
                            if hasattr(block, 'content'):
                                inner = block.content
                                if isinstance(inner, str):
                                    text = inner
                                elif isinstance(inner, list):
                                    parts = []
                                    for item in inner:
                                        if hasattr(item, 'text'):
                                            parts.append(item.text)
                                        elif isinstance(item, dict) and 'text' in item:
                                            parts.append(item['text'])
                                    text = '\n'.join(parts)
                            elif isinstance(block, dict):
                                inner = block.get('content', '')
                                if isinstance(inner, str):
                                    text = inner
                                elif isinstance(inner, list):
                                    parts = [i.get('text', '') if isinstance(i, dict) else str(i) for i in inner]
                                    text = '\n'.join(parts)
                            if text:
                                if len(text) > 5000:
                                    text = text[:5000] + "\n... [truncated]"
                                _session_logger.debug(f"[{agent_name.upper()}] TOOL_RESULT: {text}")
            except Exception:
                pass

        # User message - show in debug mode
        if _debug_logging:
            safe_echo(f"[{timestamp}] [USER] {msg.content if hasattr(msg, 'content') else str(msg)}")
            import sys
            sys.stdout.flush()
        return

    # Handle dict format (fallback for backward compatibility)
    if isinstance(msg, dict):
        msg_type = msg.get("type", "unknown")
    else:
        # Unknown object type
        if _debug_logging:
            click.echo(f"[{timestamp}] [UNKNOWN] {type(msg).__name__}: {str(msg)[:200]}")
        return

    # Legacy dict handling below
    timestamp = datetime.now().strftime("%H:%M:%S")

    if msg_type == "text":
        # Regular text output
        content = msg.get("content", "")
        if content:
            if _debug_logging:
                click.echo(f"[{timestamp}] {content}")
            else:
                click.echo(content)

    elif msg_type == "tool_use":
        # Tool execution
        tool_name = msg.get("name", "unknown")
        tool_id = msg.get("id", "")
        tool_input = msg.get("input", {})

        # Always show tool name with timestamp
        click.echo(f"[{timestamp}] [TOOL] {tool_name}", nl=False)

        # Show tool ID in debug mode
        if _debug_logging and tool_id:
            click.echo(f" (id: {tool_id[:8]}...)", nl=False)

        click.echo()  # Newline

        # Show tool input in verbose mode
        if _verbose_logging and tool_input:
            # Format tool input as readable text instead of JSON
            formatted_input = format_tool_input(tool_name, tool_input)
            # Extract just the parameters part (skip the "Tool: X" line)
            input_lines = formatted_input.split('\n')[1:]  # Skip first line
            for line in input_lines:
                click.echo(f"  {line}")

    elif msg_type == "tool_result":
        # Tool result
        if _verbose_logging:
            tool_id = msg.get("tool_use_id", "")
            content = msg.get("content", "")
            is_error = msg.get("is_error", False)

            status = "ERROR" if is_error else "SUCCESS"
            click.echo(f"[{timestamp}] [RESULT] {status}", nl=False)

            if _debug_logging and tool_id:
                click.echo(f" (id: {tool_id[:8]}...)", nl=False)

            click.echo()

            # Show result content as readable text
            if content:
                # Format result as readable text instead of JSON
                formatted_result = format_tool_result(content, is_error=is_error)
                # Extract just the content part (skip the "Result: SUCCESS/ERROR" line)
                result_lines = formatted_result.split('\n')[1:]  # Skip first line
                for line in result_lines:
                    click.echo(f"  {line}")

    elif msg_type == "error":
        # Error message
        error = msg.get("error", "Unknown error")
        click.echo(f"[{timestamp}] [ERROR] {error}", err=True)

        # Show full error details in debug mode
        if _debug_logging:
            import json
            click.echo(f"  Full error: {json.dumps(msg, indent=2)}", err=True)

    elif msg_type == "agent_start":
        # Agent session started
        agent_name = msg.get("agent", "unknown")
        click.echo(f"[{timestamp}] [AGENT] Starting: {agent_name}")

        if _verbose_logging:
            config = msg.get("config", {})
            if config:
                import json
                click.echo(f"  Config: {json.dumps(config, indent=2)}")

    elif msg_type == "agent_end":
        # Agent session ended
        agent_name = msg.get("agent", "unknown")
        status = msg.get("status", "unknown")
        click.echo(f"[{timestamp}] [AGENT] Ended: {agent_name} (status: {status})")

    else:
        # Unknown message type
        if _debug_logging:
            import json
            click.echo(f"[{timestamp}] [UNKNOWN:{msg_type}] {json.dumps(msg, indent=2)}", err=True)


def main():
    """Entry point alias; the CLI lives in squidagent.commands.main."""
    from squidagent.commands.main import main as cli_main
    cli_main()


if __name__ == '__main__':
    main()
