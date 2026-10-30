"""Custom MCP tools for git operations in SquidAgent.

This module provides custom tools for parsing git commits and querying
git metadata. These tools are exposed to Claude agents via the MCP protocol.
"""

import subprocess
import json
import shutil
from pathlib import Path
from typing import Any, Dict

from claude_agent_sdk import tool, create_sdk_mcp_server

from .parsers import (
    parse_commit_message,
    parse_task_metadata,
    parse_plan_commit,
    extract_provides_from_merge_commits,
    parse_worker_commit,
)


@tool("parse_task", "Parse task metadata from the first commit on a task branch", {"branch": str})
async def parse_task(args: Dict[str, Any]) -> Dict[str, Any]:
    """Parse task metadata from first commit on task branch.

    This tool reads the initialization commit from a task branch and
    extracts the structured metadata (ID, description, preconditions,
    provides, files, context, etc.)

    Args:
        args: Dictionary with 'branch' key (e.g., "task/001-user-model")

    Returns:
        MCP tool response with parsed metadata as JSON

    Example:
        Input: {"branch": "task/001-user-model"}
        Output: {
            "content": [{
                "type": "text",
                "text": "{\"id\": \"001\", \"description\": \"...\", ...}"
            }]
        }
    """
    branch = args.get("branch", "")

    if not branch:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({"error": "Branch name is required"}, indent=2)
            }],
            "isError": True
        }

    try:
        # Get first commit message from branch
        result = subprocess.run(
            ['git', 'log', branch, '--reverse', '--format=%B', '-n', '1'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=10
        )

        # Parse commit message into sections
        sections = parse_commit_message(result.stdout)

        # Extract task metadata
        metadata = parse_task_metadata(sections)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps(metadata, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Git command failed: {e.stderr}",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }
    except subprocess.TimeoutExpired:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Git command timed out",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }


@tool("parse_plan", "Parse execution plan from the latest commit on a plan branch", {"branch": str})
async def parse_plan(args: Dict[str, Any]) -> Dict[str, Any]:
    """Parse execution plan from latest commit on plan branch.

    This tool reads the most recent commit from a plan branch and
    extracts the execution plan including all tasks, dependencies,
    and estimates.

    Args:
        args: Dictionary with 'branch' key (e.g., "plan/session-20250101-120000")

    Returns:
        MCP tool response with parsed plan as JSON

    Example:
        Input: {"branch": "plan/session-20250101-120000"}
        Output: {
            "content": [{
                "type": "text",
                "text": "{\"session_id\": \"...\", \"tasks\": [...], ...}"
            }]
        }
    """
    branch = args.get("branch", "")

    if not branch:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({"error": "Branch name is required"}, indent=2)
            }],
            "isError": True
        }

    try:
        # Get latest commit message from plan branch
        result = subprocess.run(
            ['git', 'log', branch, '--format=%B', '-n', '1'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=10
        )

        # Parse plan commit
        plan = parse_plan_commit(result.stdout)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps(plan, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Git command failed: {e.stderr}",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }
    except subprocess.TimeoutExpired:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Git command timed out",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }


@tool("get_provides", "Get list of available preconditions from merged tasks on flow branch", {})
async def get_provides(args: Dict[str, Any]) -> Dict[str, Any]:
    """Query flow branch merge commits for available provides.

    This tool examines all merge commits on the flow branch and
    extracts the "Provides" sections to determine what capabilities
    are currently available for task preconditions.

    Args:
        args: Empty dictionary (no parameters needed)

    Returns:
        MCP tool response with list of available provides

    Example:
        Output: {
            "content": [{
                "type": "text",
                "text": "[\"User model class\", \"User.email field\", ...]"
            }]
        }
    """
    try:
        # Get all merge commit messages from flow branch
        result = subprocess.run(
            ['git', 'log', 'flow', '--merges', '--format=%B'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=30
        )

        # Extract all provides
        provides = extract_provides_from_merge_commits(result.stdout)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps(provides, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Git command failed: {e.stderr}",
                    "provides": []
                }, indent=2)
            }],
            "isError": True
        }
    except subprocess.TimeoutExpired:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Git command timed out",
                    "provides": []
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "provides": []
                }, indent=2)
            }],
            "isError": True
        }


@tool("parse_worker_commit", "Parse worker's latest commit with design and TODO progress", {"branch": str})
async def parse_worker_commit_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the latest commit on a worker's task branch.

    This tool extracts structured information from worker commits in the
    commit-only architecture where design.md and todo.md are embedded in
    commit messages.

    Args:
        args: Dictionary with 'branch' key (e.g., "task/001-user-model")

    Returns:
        MCP tool response with parsed commit data as JSON

    Example:
        Input: {"branch": "task/001-user-model"}
        Output: {
            "content": [{
                "type": "text",
                "text": "{
                    \"task_id\": \"001\",
                    \"commit_type\": \"implementation\",
                    \"step_number\": 2,
                    \"total_steps\": 6,
                    \"implementation\": \"Added User class with fields\",
                    \"design\": {
                        \"overview\": \"...\",
                        \"architecture_decisions\": [...],
                        \"interfaces_provided\": [...]
                    },
                    \"todo_list\": [
                        {\"number\": 1, \"description\": \"...\", \"completed\": true},
                        {\"number\": 2, \"description\": \"...\", \"completed\": true},
                        {\"number\": 3, \"description\": \"...\", \"completed\": false}
                    ],
                    \"progress\": {
                        \"status\": \"in_progress\",
                        \"completed\": 2,
                        \"total\": 6
                    }
                }"
            }]
        }
    """
    branch = args.get("branch", "")

    if not branch:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({"error": "branch parameter is required"}, indent=2)
            }],
            "isError": True
        }

    try:
        # Get latest commit message on the branch
        result = subprocess.run(
            ['git', 'log', branch, '-n', '1', '--format=%B'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=10
        )

        commit_message = result.stdout

        if not commit_message.strip():
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": f"No commits found on branch {branch}"
                    }, indent=2)
                }],
                "isError": True
            }

        # Parse the commit message
        parsed = parse_worker_commit(commit_message)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps(parsed, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Failed to read commit from branch: {e.stderr}",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }
    except subprocess.TimeoutExpired:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Git command timed out",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "branch": branch
                }, indent=2)
            }],
            "isError": True
        }


def run_scheduler_on_tasks(
    tasks: list,
    max_parallel: int = 6,
    convention_tokens: int = 0,
    layer_convention_tokens: dict = None,
    bypass_threshold: int = 0,
    agent_tool: bool = False,
) -> dict | None:
    """Run ParallelizabilityScheduler on a task list. Pure computation, no SDK dependency.

    Args:
        tasks: List of task dicts with 'id' and 'estimated_output_tokens' fields.
        max_parallel: Maximum parallel workers.
        convention_tokens: Fallback alignment-cost estimate (used if layer_convention_tokens not provided).
        layer_convention_tokens: Per-layer alignment cost, e.g. {"L0": 3000, "L1": 5000}.
        bypass_threshold: Per-layer bypass threshold (0 = disabled).
        agent_tool: Word the conventions guidance for agent-tool mode.

    Returns:
        Schedule result dict with 'schedule', 'decisions', 'n_waves', '_waves_raw',
        or None on failure.
    """
    from .scheduler import ParallelizabilityScheduler

    scheduler = ParallelizabilityScheduler(max_parallel=max_parallel,
                                           bypass_threshold=bypass_threshold,
                                           agent_tool=agent_tool)
    waves, group_decisions = scheduler.create_schedule(
        tasks, convention_planning_tokens=convention_tokens or 0,
        layer_convention_tokens=layer_convention_tokens,
    )

    compact_schedule = []
    for w in waves:
        entry = {
            "wave": w.wave_index,
            "task_ids": w.task_ids,
            "strategy": w.strategy.value,
        }
        if w.conventions_prompt:
            entry["conventions_prompt"] = w.conventions_prompt
        compact_schedule.append(entry)

    decision_summary = [
        f"{d['group_id']}: {d['task_ids']} -> {d['strategy']} (ratio={d['ratio']})"
        for d in group_decisions
    ]

    return {
        "schedule": compact_schedule,
        "decisions": decision_summary,
        "n_waves": len(waves),
        "_waves_raw": [w.to_dict() for w in waves],
    }


# Plan-only mode (agent-tool execution): create_plan_branch runs the scheduler and
# returns the schedule without touching git. Enabled by agent_tool.plan().
_PLAN_ONLY = {"enabled": False, "max_parallel": 6, "bypass_threshold": 0, "result": None}


def set_plan_only(enabled: bool, max_parallel: int = 6, bypass_threshold: int = 0) -> None:
    """Enable/disable plan-only mode for create_plan_branch."""
    _PLAN_ONLY.update(enabled=enabled, max_parallel=max_parallel,
                      bypass_threshold=bypass_threshold, result=None)


def get_plan_only_result() -> dict | None:
    """Plan data recorded by the last plan-only create_plan_branch call."""
    return _PLAN_ONLY["result"]


def _plan_only_response(session_id: str, tasks, args: Dict[str, Any]) -> Dict[str, Any]:
    """Lightweight create_plan_branch: parse tasks, run scheduler, return schedule only."""
    if isinstance(tasks, str):
        tasks = json.loads(tasks)
    convention_planning_tokens = args.get("convention_planning_tokens", 0)
    layer_convention_tokens = args.get("layer_convention_tokens", None)

    schedule_result = run_scheduler_on_tasks(
        tasks, max_parallel=_PLAN_ONLY["max_parallel"],
        convention_tokens=convention_planning_tokens or 0,
        layer_convention_tokens=layer_convention_tokens,
        bypass_threshold=_PLAN_ONLY["bypass_threshold"],
        agent_tool=True,
    )
    result = {
        "success": True,
        "branch_name": f"plan/{session_id}",
        "mode": "agent-tool (no git)",
        "tasks": tasks if isinstance(tasks, list) else [],
        "convention_planning_tokens": convention_planning_tokens,
        "layer_convention_tokens": layer_convention_tokens,
    }
    if schedule_result:
        result["schedule"] = schedule_result
    _PLAN_ONLY["result"] = result

    # Persist for inspection: full plan data + appended scheduler decisions
    try:
        metrics_dir = Path(".squidagent/metrics")
        metrics_dir.mkdir(parents=True, exist_ok=True)
        (metrics_dir / "schedule.json").write_text(json.dumps(result, indent=2))
        if schedule_result:
            trace = Path(".squidagent/sched_trace.json")
            history = json.loads(trace.read_text()) if trace.exists() else []
            history.append({
                "branch": result["branch_name"],
                "decisions": schedule_result.get("decisions", []),
                "schedule": schedule_result.get("schedule", []),
                "layer_convention_tokens": layer_convention_tokens,
            })
            trace.write_text(json.dumps(history, indent=2))
    except Exception:
        pass

    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


@tool(
    "create_plan_branch",
    "Create plan branch with metadata commit",
    {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "user_request": {"type": "string"},
            "architecture": {"type": "string"},
            "design_doc": {"type": "string"},
            "technology_stack": {"type": "string"},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Numeric string, zero-padded (e.g. '001')"},
                        "description": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "preconditions": {"type": "array", "items": {"type": "string"}},
                        "provides": {"type": "array", "items": {"type": "string"}},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "estimated_time": {"type": "string"},
                        "estimated_output_tokens": {"type": "number", "description": "REQUIRED. Estimated LLM output tokens for this task. Used by scheduler to decide parallel vs serial."},
                        "priority": {"type": "string"}
                    },
                    "required": ["id", "description", "estimated_output_tokens"]
                }
            },
            "estimated_total_time": {"type": "string"},
            "convention_planning_tokens": {"type": "number", "description": "DEPRECATED: use layer_convention_tokens instead. Fallback value if layer_convention_tokens is not provided."},
            "layer_convention_tokens": {"type": "object", "description": "Per-layer convention planning tokens, e.g. {\"L0\": 3000, \"L1\": 5000}. Each layer may have different alignment cost.", "additionalProperties": {"type": "number"}},
            "dependency_graph": {"type": "string"}
        },
        "required": ["session_id", "user_request", "tasks", "dependency_graph"]
    }
)
async def create_plan_branch(args: Dict[str, Any]) -> Dict[str, Any]:
    """Create plan branch with initial plan commit.

    Creates branch plan/session-{session_id} from flow, commits plan metadata
    in exact parsers.py format, and returns to original branch.

    Args:
        args: Dictionary with required fields:
            - session_id: e.g., "session-20250106-140530"
            - user_request: Original user request
            - architecture: Architecture description
            - design_doc: Design document description
            - technology_stack: Technology stack description
            - tasks: List of task dicts with full metadata
            - estimated_total_time: e.g., "45 minutes"
            - dependency_graph: Task dependency relationships and execution order

    Returns:
        MCP tool response with success status, branch name, and commit SHA
    """
    session_id = args.get("session_id", "")
    user_request = args.get("user_request", "")
    architecture = args.get("architecture", "")
    design_doc = args.get("design_doc", "")
    technology_stack = args.get("technology_stack", "")
    tasks = args.get("tasks", [])
    estimated_total_time = args.get("estimated_total_time", "")
    dependency_graph = args.get("dependency_graph", "")

    # Agent-tool mode: skip all git operations, only run the scheduler
    if _PLAN_ONLY["enabled"]:
        return _plan_only_response(session_id, tasks, args)

    # Validate required fields
    if not session_id:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "session_id is required"}, indent=2)}],
            "isError": True
        }
    if not user_request:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "user_request is required"}, indent=2)}],
            "isError": True
        }
    if not tasks:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "tasks list is required"}, indent=2)}],
            "isError": True
        }

    # Validate dependency graph is not empty
    if not dependency_graph or not dependency_graph.strip():
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": "dependency_graph is required and cannot be empty",
                "hint": "Describe task execution order, e.g.: 'Ready immediately: task-001, task-002\\nAfter task-001: task-003 becomes ready'"
            }, indent=2)}],
            "isError": True
        }

    # Validate each task has required fields
    for i, task in enumerate(tasks):
        # Check ID exists and is non-empty
        task_id = task.get("id", "").strip()
        if not task_id:
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "error": f"Task #{i + 1} is missing required 'id' field",
                    "hint": "Each task must have 'id' (numeric string like '001'), 'description', 'preconditions' (list), 'provides' (list), and 'files' (list)",
                    "failed_task": task
                }, indent=2)}],
                "isError": True
            }

        # Check ID format (should be digits, optionally followed by a letter)
        import re
        if not re.match(r'^\d+[a-z]?$', task_id):
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "error": f"Task #{i + 1} has invalid ID '{task_id}' (must be digits like '001' or '001a')",
                    "hint": "Task IDs should be numeric strings like '001', '002', '003' or '001a', '001b'",
                    "failed_task": task
                }, indent=2)}],
                "isError": True
            }

        # Check description
        description = task.get("description", "").strip()
        if not description:
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "error": f"Task {task_id} is missing required 'description' field",
                    "failed_task": task
                }, indent=2)}],
                "isError": True
            }

        # Preconditions and provides must be lists (can be empty)
        if not isinstance(task.get("preconditions", []), list):
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "error": f"Task {task_id} 'preconditions' must be a list (can be empty list [])",
                    "failed_task": task
                }, indent=2)}],
                "isError": True
            }

        if not isinstance(task.get("provides", []), list):
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "error": f"Task {task_id} 'provides' must be a list (can be empty list [])",
                    "failed_task": task
                }, indent=2)}],
                "isError": True
            }

    branch_name = f"plan/{session_id}"

    try:
        # Validate this is a git repository
        repo_check = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True,
            timeout=5
        )
        if repo_check.returncode != 0:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": "Not a git repository",
                        "hint": "The current directory is not initialized as a git repository.",
                        "action_required": "Initialize git repository first: git init",
                        "current_directory": str(Path.cwd())
                    }, indent=2)
                }],
                "isError": True
            }

        # Check if branch already exists
        check_result = subprocess.run(
            ['git', 'rev-parse', '--verify', branch_name],
            capture_output=True,
            timeout=5
        )
        if check_result.returncode == 0:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": f"Branch {branch_name} already exists",
                        "hint": "Use a different session_id or delete the existing branch first"
                    }, indent=2)
                }],
                "isError": True
            }

        # Store current branch to return to it later
        current_branch_result = subprocess.run(
            ['git', 'branch', '--show-current'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=5
        )
        current_branch = current_branch_result.stdout.strip() or "flow"

        # Create plan branch from flow (force checkout flow first)
        subprocess.run(
            ['git', 'checkout', '-f', 'flow'],
            capture_output=True,
            check=True,
            timeout=10
        )
        subprocess.run(
            ['git', 'checkout', '-b', branch_name],
            capture_output=True,
            check=True,
            timeout=10
        )

        # Copy instruction files from squidagent/prompts/ to .squidagent/
        prompts_dir = Path(__file__).parent / "prompts"
        squid_dir = Path.cwd() / ".squidagent"
        squid_dir.mkdir(exist_ok=True)

        instruction_files = [
            ("orchestrator.md", "ORCHESTRATOR_INSTRUCTIONS.md"),
            ("worker.md", "WORKER_INSTRUCTIONS.md"),
        ]

        copied_files = []
        for source_file, dest_file in instruction_files:
            source_path = prompts_dir / source_file
            dest_path = squid_dir / dest_file
            if source_path.exists():
                shutil.copy2(source_path, dest_path)
                copied_files.append(str(dest_path.relative_to(Path.cwd())))
            else:
                # Rollback on error
                subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)
                subprocess.run(['git', 'branch', '-D', branch_name], capture_output=True, timeout=5)
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "error": f"Source file not found: {source_path}",
                            "rollback": "Branch creation rolled back"
                        }, indent=2)
                    }],
                    "isError": True
                }

        # Stage instruction files
        subprocess.run(
            ['git', 'add'] + copied_files,
            capture_output=True,
            check=True,
            timeout=10
        )

        # Build plan commit message
        from datetime import datetime
        created_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Build tasks section
        tasks_section = ""
        for task in tasks:
            task_id = task.get("id", "")
            description = task.get("description", "")
            status = task.get("status", "pending")
            preconditions = task.get("preconditions", [])
            provides = task.get("provides", [])
            files = task.get("files", [])
            estimated_time = task.get("estimated_time", "")
            priority = task.get("priority", "medium")

            depends_on = task.get("depends_on", [])

            tasks_section += f"\n### Task {task_id}\n"
            tasks_section += f"ID: {task_id}\n"
            tasks_section += f"Description: {description}\n"
            tasks_section += f"Status: {status}\n"

            if depends_on:
                tasks_section += "Depends on:\n"
                for dep in depends_on:
                    tasks_section += f"  - {dep}\n"
            else:
                tasks_section += "Depends on: []\n"

            if preconditions:
                tasks_section += "Preconditions:\n"
                for pre in preconditions:
                    tasks_section += f"  - {pre}\n"
            else:
                tasks_section += "Preconditions: []\n"

            if provides:
                tasks_section += "Provides:\n"
                for prov in provides:
                    tasks_section += f"  - {prov}\n"
            else:
                tasks_section += "Provides: []\n"

            if files:
                tasks_section += "Files:\n"
                for file in files:
                    tasks_section += f"  - {file}\n"

            tasks_section += f"Estimated Time: {estimated_time}\n"
            estimated_output_tokens = task.get("estimated_output_tokens", 0)
            if estimated_output_tokens:
                tasks_section += f"Estimated Output Tokens: {estimated_output_tokens}\n"
            tasks_section += f"Priority: {priority}\n"

        total_tasks = len(tasks)
        completed_tasks = sum(1 for t in tasks if t.get("status") == "completed")

        convention_planning_tokens = args.get("convention_planning_tokens", 0)
        # Per-layer alignment costs {"L0":.., "L1":..}. The worktree path must pass
        # these to the scheduler too, else it silently uses a flat default for every
        # layer — the inconsistency with the paper's per-layer Ĉ_alg(L_k).
        layer_convention_tokens = args.get("layer_convention_tokens", None)

        estimates_section = f"Estimated Total Time: {estimated_total_time}\n"
        if convention_planning_tokens:
            estimates_section += f"Convention Planning Tokens: {convention_planning_tokens}\n"
        estimates_section += f"Total Tasks: {total_tasks}\n"
        estimates_section += f"Completed: {completed_tasks}/{total_tasks} tasks"

        commit_message = f"""Initialize execution plan v1

## Session Information
Session ID: {session_id}
User Request: {user_request}
Created: {created_timestamp}
Plan Branch: {branch_name}
Plan Version: v1

## Architecture
{architecture}

## Design Document
{design_doc}

## Technology Stack
{technology_stack}

## Tasks{tasks_section}

## Estimates
{estimates_section}

## Dependency Graph
{dependency_graph}
"""

        # Create plan commit
        subprocess.run(
            ['git', 'commit', '--allow-empty', '-m', commit_message],
            capture_output=True,
            check=True,
            timeout=10
        )

        # Get commit SHA
        commit_sha_result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=5
        )
        commit_sha = commit_sha_result.stdout.strip()

        # Return to original branch
        subprocess.run(
            ['git', 'checkout', current_branch],
            capture_output=True,
            check=True,
            timeout=10
        )

        # Auto-run scheduler (saves one LLM round-trip vs separate create_schedule call)
        schedule_result = None
        try:
            from .sdk_workers import get_sdk_worker_manager
            mgr = get_sdk_worker_manager()
            max_par = mgr.max_parallel if mgr else 6
            conv_tokens = convention_planning_tokens or 0
            schedule_result = run_scheduler_on_tasks(
                tasks, max_parallel=max_par, convention_tokens=conv_tokens,
                layer_convention_tokens=layer_convention_tokens,
            )
            # Store schedule in worker manager for wave tracking
            if mgr and schedule_result:
                mgr.set_schedule(schedule_result.get("_waves_raw", []))
        except Exception:
            pass  # scheduler failure is non-fatal

        # Persist the scheduler's per-layer decisions (with ratios) for later
        # inspection. Appended (not overwritten) so dynamic replans accumulate.
        if schedule_result:
            try:
                trace = Path(".squidagent/sched_trace.json")
                trace.parent.mkdir(parents=True, exist_ok=True)
                history = json.loads(trace.read_text()) if trace.exists() else []
                history.append({
                    "branch": branch_name,
                    "decisions": schedule_result.get("decisions", []),
                    "schedule": schedule_result.get("schedule", []),
                    "layer_convention_tokens": layer_convention_tokens,
                })
                trace.write_text(json.dumps(history, indent=2))
            except Exception:
                pass

        result = {
            "success": True,
            "branch_name": branch_name,
            "commit_sha": commit_sha,
        }
        if schedule_result:
            result["schedule"] = schedule_result

        # Worker launch is handled by the orchestrator calling launch_wave separately.
        # Auto-launching from create_plan_branch was tried but creates lifecycle issues:
        # the orchestrator doesn't know about side-effect-launched workers and may
        # duplicate launches.

        return {
            "content": [{
                "type": "text",
                "text": json.dumps(result, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        # Attempt rollback
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)
        subprocess.run(['git', 'branch', '-D', branch_name], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Git command failed: {e.stderr.decode() if e.stderr else str(e)}",
                    "rollback": "Attempted to rollback branch creation"
                }, indent=2)
            }],
            "isError": True
        }
    except subprocess.TimeoutExpired:
        # Attempt rollback
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)
        subprocess.run(['git', 'branch', '-D', branch_name], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Git command timed out",
                    "rollback": "Attempted to rollback branch creation"
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        # Attempt rollback
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)
        subprocess.run(['git', 'branch', '-D', branch_name], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "rollback": "Attempted to rollback branch creation"
                }, indent=2)
            }],
            "isError": True
        }


@tool(
    "create_task_branch",
    "Create task branch with metadata commit",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "branch_slug": {"type": "string"},
            "description": {"type": "string"},
            "preconditions": {"type": "array"},
            "provides": {"type": "array"},
            "files": {"type": "array"},
            "session_goal": {"type": "string"},
            "session_id": {"type": "string"},
            "plan_branch": {"type": "string"},
            "plan_version": {"type": "string"},
            "depends_on": {"type": "array"},
            "enables": {"type": "array"},
            "parallel_with": {"type": "array"},
            "completed_tasks": {"type": "array"},
            "estimated_time": {"type": "string"},
            "priority": {"type": "string"}
        },
        "required": ["task_id", "branch_slug", "description", "preconditions", "provides", "files", "session_goal", "session_id", "plan_branch", "plan_version"]
    }
)
async def create_task_branch(args: Dict[str, Any]) -> Dict[str, Any]:
    """Create task branch with task metadata commit.

    Creates branch task/{task_id}-{branch_slug} from flow, commits task metadata
    in exact parsers.py format, and returns to original branch.

    Args:
        args: Dictionary with required fields:
            - task_id: e.g., "001"
            - branch_slug: e.g., "user-model"
            - description: Task description
            - preconditions: List of precondition strings
            - provides: List of capability strings
            - files: List of file paths
            - session_goal: Overall session goal
            - session_id: e.g., "session-20250106-140530"
            - plan_branch: e.g., "plan/session-20250106-140530"
            - plan_version: e.g., "v1"
            - depends_on: List of task dependencies
            - enables: List of tasks this enables
            - parallel_with: List of parallel tasks
            - completed_tasks: List of completed task IDs
            - estimated_time: e.g., "8 minutes"
            - priority: e.g., "high"

    Returns:
        MCP tool response with success status, branch name, and commit SHA
    """
    task_id = args.get("task_id", "")
    branch_slug = args.get("branch_slug", "")
    description = args.get("description", "")
    preconditions = args.get("preconditions", [])
    provides = args.get("provides", [])
    files = args.get("files", [])
    session_goal = args.get("session_goal", "")
    session_id = args.get("session_id", "")
    plan_branch = args.get("plan_branch", "")
    plan_version = args.get("plan_version", "v1")
    depends_on = args.get("depends_on", [])
    enables = args.get("enables", [])
    parallel_with = args.get("parallel_with", [])
    completed_tasks = args.get("completed_tasks", [])
    estimated_time = args.get("estimated_time", "")
    priority = args.get("priority", "medium")

    # Validate required fields
    if not task_id:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "task_id is required"}, indent=2)}],
            "isError": True
        }

    # Validate task ID format (should be digits, optionally followed by a letter)
    import re
    if not re.match(r'^\d+[a-z]?$', task_id.strip()):
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"Invalid task ID '{task_id}' (must be digits like '001' or '001a')",
                "hint": "Task IDs should be numeric strings like '001', '002', '003' or '001a', '001b'"
            }, indent=2)}],
            "isError": True
        }

    if not branch_slug:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "branch_slug is required"}, indent=2)}],
            "isError": True
        }
    if not description:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "description is required"}, indent=2)}],
            "isError": True
        }

    # Validate preconditions and provides are lists
    if not isinstance(preconditions, list):
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": "'preconditions' must be a list (can be empty list [])",
                "received_type": type(preconditions).__name__
            }, indent=2)}],
            "isError": True
        }

    if not isinstance(provides, list):
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": "'provides' must be a list (can be empty list [])",
                "received_type": type(provides).__name__
            }, indent=2)}],
            "isError": True
        }

    branch_name = f"task/{task_id}-{branch_slug}"

    try:
        # Check if branch already exists
        check_result = subprocess.run(
            ['git', 'rev-parse', '--verify', branch_name],
            capture_output=True,
            timeout=5
        )
        if check_result.returncode == 0:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": f"Branch {branch_name} already exists",
                        "hint": "Use a different task_id or branch_slug, or delete the existing branch first"
                    }, indent=2)
                }],
                "isError": True
            }

        # Store current branch to return to it later
        current_branch_result = subprocess.run(
            ['git', 'branch', '--show-current'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=5
        )
        current_branch = current_branch_result.stdout.strip() or "flow"

        # Create task branch from flow (force checkout flow first)
        subprocess.run(
            ['git', 'checkout', '-f', 'flow'],
            capture_output=True,
            check=True,
            timeout=10
        )
        subprocess.run(
            ['git', 'checkout', '-b', branch_name],
            capture_output=True,
            check=True,
            timeout=10
        )

        # Copy instruction files from squidagent/prompts/ to .squidagent/
        prompts_dir = Path(__file__).parent / "prompts"
        squid_dir = Path.cwd() / ".squidagent"
        squid_dir.mkdir(exist_ok=True)

        instruction_files = [
            ("orchestrator.md", "ORCHESTRATOR_INSTRUCTIONS.md"),
            ("worker.md", "WORKER_INSTRUCTIONS.md"),
        ]

        copied_files = []
        for source_file, dest_file in instruction_files:
            source_path = prompts_dir / source_file
            dest_path = squid_dir / dest_file
            if source_path.exists():
                shutil.copy2(source_path, dest_path)
                copied_files.append(str(dest_path.relative_to(Path.cwd())))
            else:
                # Rollback on error
                subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)
                subprocess.run(['git', 'branch', '-D', branch_name], capture_output=True, timeout=5)
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "error": f"Source file not found: {source_path}",
                            "rollback": "Branch creation rolled back"
                        }, indent=2)
                    }],
                    "isError": True
                }

        # Stage instruction files
        subprocess.run(
            ['git', 'add'] + copied_files,
            capture_output=True,
            check=True,
            timeout=10
        )

        # Build preconditions section
        preconditions_section = ""
        if preconditions:
            preconditions_section = "Preconditions:\n"
            for pre in preconditions:
                preconditions_section += f"  - {pre}\n"
        else:
            preconditions_section = "Preconditions: []\n"

        # Build provides section
        provides_section = ""
        if provides:
            provides_section = "Provides:\n"
            for prov in provides:
                provides_section += f"  - {prov}\n"
        else:
            provides_section = "Provides: []\n"

        # Build files section
        files_section = ""
        if files:
            files_section = "Files to modify:\n"
            for file in files:
                files_section += f"  - {file}\n"
        else:
            files_section = "Files to modify: []\n"

        # Build context section
        depends_on_str = str(depends_on) if depends_on else "[]"
        enables_str = str(enables) if enables else "[]"
        parallel_with_str = str(parallel_with) if parallel_with else "[]"
        completed_tasks_str = str(completed_tasks) if completed_tasks else "[]"

        commit_message = f"""Initialize task/{task_id}-{branch_slug}

## Task Metadata
ID: {task_id}
Description: {description}
Status: pending

## Dependencies
{preconditions_section}
{provides_section}

## Files
{files_section}

## Context
Session Goal: {session_goal}
Session ID: {session_id}
Plan Branch: {plan_branch}
Plan Version: {plan_version}
Depends on: {depends_on_str}
Enables: {enables_str}
Parallel with: {parallel_with_str}
Completed Tasks: {completed_tasks_str}

## Estimates
Estimated Time: {estimated_time}
Priority: {priority}
"""

        # Create task metadata commit
        subprocess.run(
            ['git', 'commit', '--allow-empty', '-m', commit_message],
            capture_output=True,
            check=True,
            timeout=10
        )

        # Get commit SHA
        commit_sha_result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=5
        )
        commit_sha = commit_sha_result.stdout.strip()

        # Return to original branch
        subprocess.run(
            ['git', 'checkout', current_branch],
            capture_output=True,
            check=True,
            timeout=10
        )

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "branch_name": branch_name,
                    "commit_sha": commit_sha
                }, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        # Attempt rollback
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)
        subprocess.run(['git', 'branch', '-D', branch_name], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Git command failed: {e.stderr.decode() if e.stderr else str(e)}",
                    "rollback": "Attempted to rollback branch creation"
                }, indent=2)
            }],
            "isError": True
        }
    except subprocess.TimeoutExpired:
        # Attempt rollback
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)
        subprocess.run(['git', 'branch', '-D', branch_name], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Git command timed out",
                    "rollback": "Attempted to rollback branch creation"
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        # Attempt rollback
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)
        subprocess.run(['git', 'branch', '-D', branch_name], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "rollback": "Attempted to rollback branch creation"
                }, indent=2)
            }],
            "isError": True
        }


@tool(
    "update_plan_branch",
    "Update plan commit with completed tasks and dynamically discovered new tasks",
    {
        "type": "object",
        "properties": {
            "plan_branch": {"type": "string"},
            "completed_task_ids": {"type": "array"},
            "new_tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "preconditions": {"type": "array", "items": {"type": "string"}},
                        "provides": {"type": "array", "items": {"type": "string"}},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "estimated_time": {"type": "string"},
                        "estimated_output_tokens": {"type": "number", "description": "REQUIRED. Estimated LLM output tokens."},
                        "priority": {"type": "string"}
                    },
                    "required": ["id", "description", "estimated_output_tokens"]
                }
            },
            "architecture_updates": {"type": "string"}
        },
        "required": ["plan_branch"]
    }
)
async def update_plan_branch(args: Dict[str, Any]) -> Dict[str, Any]:
    """Update plan branch with completed task status and dynamically discovered new tasks.

    This tool updates an existing plan branch by:
    1. Checking out plan branch
    2. Parsing current plan commit
    3. Marking tasks as completed
    4. Appending architecture learnings
    5. Adding new tasks discovered during implementation
    6. Creating new plan commit with incremented version
    7. Returning to original branch

    Args:
        args: Dictionary with fields:
            - plan_branch: e.g., "plan/session-20250106-140530"
            - completed_task_ids: List of task IDs to mark complete (optional)
            - new_tasks: List of new task dicts (optional)
            - architecture_updates: Additional learnings to append (optional)

    Returns:
        MCP tool response with success status, plan version, and commit SHA
    """
    plan_branch = args.get("plan_branch", "")
    completed_task_ids = args.get("completed_task_ids", [])
    new_tasks = args.get("new_tasks", [])
    architecture_updates = args.get("architecture_updates", "")

    # Validate required fields
    if not plan_branch:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "plan_branch is required"}, indent=2)}],
            "isError": True
        }

    try:
        # Store current branch to return to it later
        current_branch_result = subprocess.run(
            ['git', 'branch', '--show-current'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=5
        )
        current_branch = current_branch_result.stdout.strip() or "flow"

        # Checkout plan branch
        subprocess.run(
            ['git', 'checkout', plan_branch],
            capture_output=True,
            check=True,
            timeout=10
        )

        # Get current plan commit
        commit_result = subprocess.run(
            ['git', 'log', '-n', '1', '--format=%B'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=10
        )

        # Parse current plan
        current_plan = parse_plan_commit(commit_result.stdout)

        # Determine new version by counting commits on plan branch
        version_result = subprocess.run(
            ['git', 'rev-list', '--count', plan_branch],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=5
        )
        commit_count = int(version_result.stdout.strip())
        new_version = f"v{commit_count + 1}"

        # Update task statuses
        tasks = current_plan.get("tasks", [])
        for task in tasks:
            if task.get("id") in completed_task_ids:
                task["status"] = "completed"

        # Add new tasks if provided
        if new_tasks:
            # Validate new tasks before adding them
            import re
            for i, task in enumerate(new_tasks):
                # Check ID exists and is non-empty
                task_id = task.get("id", "").strip()
                if not task_id:
                    return {
                        "content": [{"type": "text", "text": json.dumps({
                            "error": f"New task #{i + 1} is missing required 'id' field",
                            "hint": "Each task must have 'id' (numeric string like '001')",
                            "failed_task": task
                        }, indent=2)}],
                        "isError": True
                    }

                # Check ID format
                if not re.match(r'^\d+[a-z]?$', task_id):
                    return {
                        "content": [{"type": "text", "text": json.dumps({
                            "error": f"New task #{i + 1} has invalid ID '{task_id}'",
                            "hint": "Task IDs should be numeric strings like '001', '002', '003'",
                            "failed_task": task
                        }, indent=2)}],
                        "isError": True
                    }

                # Check description
                if not task.get("description", "").strip():
                    return {
                        "content": [{"type": "text", "text": json.dumps({
                            "error": f"New task {task_id} is missing required 'description' field",
                            "failed_task": task
                        }, indent=2)}],
                        "isError": True
                    }

                # Validate list fields
                if not isinstance(task.get("preconditions", []), list):
                    return {
                        "content": [{"type": "text", "text": json.dumps({
                            "error": f"New task {task_id} 'preconditions' must be a list",
                            "failed_task": task
                        }, indent=2)}],
                        "isError": True
                    }

                if not isinstance(task.get("provides", []), list):
                    return {
                        "content": [{"type": "text", "text": json.dumps({
                            "error": f"New task {task_id} 'provides' must be a list",
                            "failed_task": task
                        }, indent=2)}],
                        "isError": True
                    }

            tasks.extend(new_tasks)

        # Update architecture with learnings
        architecture = current_plan.get("architecture", "")
        if architecture_updates:
            architecture += f"\n\n### Progress Update {commit_count}:\n{architecture_updates}"

        # Calculate stats
        total_tasks = len(tasks)
        completed_count = sum(1 for t in tasks if t.get("status") == "completed")

        # Build tasks section
        tasks_section = ""
        for task in tasks:
            task_id = task.get("id", "")
            description = task.get("description", "")
            status = task.get("status", "pending")
            preconditions = task.get("preconditions", [])
            provides = task.get("provides", [])
            files = task.get("files", [])
            estimated_time = task.get("estimated_time", "")
            priority = task.get("priority", "medium")

            depends_on = task.get("depends_on", [])

            tasks_section += f"\n### Task {task_id}\n"
            tasks_section += f"ID: {task_id}\n"
            tasks_section += f"Description: {description}\n"
            tasks_section += f"Status: {status}\n"

            if depends_on:
                tasks_section += "Depends on:\n"
                for dep in depends_on:
                    tasks_section += f"  - {dep}\n"
            else:
                tasks_section += "Depends on: []\n"

            if preconditions:
                tasks_section += "Preconditions:\n"
                for pre in preconditions:
                    tasks_section += f"  - {pre}\n"
            else:
                tasks_section += "Preconditions: []\n"

            if provides:
                tasks_section += "Provides:\n"
                for prov in provides:
                    tasks_section += f"  - {prov}\n"
            else:
                tasks_section += "Provides: []\n"

            if files:
                tasks_section += "Files:\n"
                for file in files:
                    tasks_section += f"  - {file}\n"

            tasks_section += f"Estimated Time: {estimated_time}\n"
            estimated_output_tokens = task.get("estimated_output_tokens", 0)
            if estimated_output_tokens:
                tasks_section += f"Estimated Output Tokens: {estimated_output_tokens}\n"
            tasks_section += f"Priority: {priority}\n"

        # Build updated plan commit
        from datetime import datetime
        created_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        commit_message = f"""Update execution plan {new_version}

## Session Information
Session ID: {current_plan.get('session_id', '')}
User Request: {current_plan.get('user_request', '')}
Created: {created_timestamp}
Plan Branch: {plan_branch}
Plan Version: {new_version}

## Architecture
{architecture}

## Design Document
{current_plan.get('design_doc', '')}

## Technology Stack
{current_plan.get('technology_stack', '')}

## Tasks{tasks_section}

## Estimates
Estimated Total Time: {current_plan.get('estimated_total_time', '')}
Total Tasks: {total_tasks}
Completed: {completed_count}/{total_tasks} tasks

## Dependency Graph
{current_plan.get('dependency_graph', '')}
"""

        # Create update commit
        subprocess.run(
            ['git', 'commit', '--allow-empty', '-m', commit_message],
            capture_output=True,
            check=True,
            timeout=10
        )

        # Get commit SHA
        commit_sha_result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
            timeout=5
        )
        commit_sha = commit_sha_result.stdout.strip()

        # Return to original branch
        subprocess.run(
            ['git', 'checkout', current_branch],
            capture_output=True,
            check=True,
            timeout=10
        )

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "plan_version": new_version,
                    "commit_sha": commit_sha,
                    "total_tasks": total_tasks,
                    "completed_tasks": completed_count
                }, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        # Attempt to return to original branch
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Git command failed: {e.stderr.decode() if e.stderr else str(e)}",
                    "plan_branch": plan_branch
                }, indent=2)
            }],
            "isError": True
        }
    except subprocess.TimeoutExpired:
        # Attempt to return to original branch
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Git command timed out",
                    "plan_branch": plan_branch
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        # Attempt to return to original branch
        subprocess.run(['git', 'checkout', current_branch], capture_output=True, timeout=5)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "plan_branch": plan_branch
                }, indent=2)
            }],
            "isError": True
        }


@tool(
    "create_worktree",
    "Create a git worktree for parallel worker execution",
    {
        "type": "object",
        "properties": {
            "worker_id": {"type": "string", "description": "Worker ID (e.g., '1', '2', '3')"},
            "task_branch": {"type": "string", "description": "Task branch name (e.g., 'task/001-user-model')"}
        },
        "required": ["worker_id", "task_branch"]
    }
)
async def create_worktree(args: Dict[str, Any]) -> Dict[str, Any]:
    """Create a git worktree for isolated parallel execution.

    Creates worktree at `.worktrees/worker-{worker_id}` linked to task_branch.
    If worktree already exists, removes it first.

    Args:
        args: Dictionary with fields:
            - worker_id: Worker identifier (e.g., "1", "2")
            - task_branch: Task branch to check out (e.g., "task/001-user-model")

    Returns:
        MCP tool response with success status and worktree path
    """
    worker_id = args.get("worker_id", "")
    task_branch = args.get("task_branch", "")

    if not worker_id or not task_branch:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": "worker_id and task_branch are required"
            }, indent=2)}],
            "isError": True
        }

    worktree_path = f".worktrees/worker-{worker_id}"

    try:
        # Check if worktree already exists and remove it
        list_result = subprocess.run(
            ['git', 'worktree', 'list'],
            capture_output=True,
            text=True,
            timeout=5
        )

        if worktree_path in list_result.stdout:
            # Remove existing worktree
            subprocess.run(
                ['git', 'worktree', 'remove', '--force', worktree_path],
                capture_output=True,
                text=True,
                timeout=5,
                check=True
            )

        # Create worktree
        result = subprocess.run(
            ['git', 'worktree', 'add', worktree_path, task_branch],
            capture_output=True,
            text=True,
            timeout=10,
            check=True
        )

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "worktree_path": worktree_path,
                    "task_branch": task_branch,
                    "worker_id": worker_id,
                    "message": f"Created worktree at {worktree_path} for {task_branch}"
                }, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Failed to create worktree: {e.stderr}",
                    "worktree_path": worktree_path,
                    "task_branch": task_branch
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "worktree_path": worktree_path
                }, indent=2)
            }],
            "isError": True
        }


@tool(
    "remove_worktree",
    "Remove a git worktree after worker completes",
    {
        "type": "object",
        "properties": {
            "worker_id": {"type": "string", "description": "Worker ID (e.g., '1', '2', '3')"}
        },
        "required": ["worker_id"]
    }
)
async def remove_worktree(args: Dict[str, Any]) -> Dict[str, Any]:
    """Remove a git worktree after worker completes task.

    Removes worktree at `.worktrees/worker-{worker_id}`.
    Uses --force to ensure removal even if worktree has uncommitted changes.

    Args:
        args: Dictionary with fields:
            - worker_id: Worker identifier (e.g., "1", "2")

    Returns:
        MCP tool response with success status
    """
    worker_id = args.get("worker_id", "")

    if not worker_id:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": "worker_id is required"
            }, indent=2)}],
            "isError": True
        }

    worktree_path = f".worktrees/worker-{worker_id}"

    try:
        # Remove worktree with --force
        result = subprocess.run(
            ['git', 'worktree', 'remove', '--force', worktree_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=True
        )

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "worktree_path": worktree_path,
                    "worker_id": worker_id,
                    "message": f"Removed worktree at {worktree_path}"
                }, indent=2)
            }]
        }

    except subprocess.CalledProcessError as e:
        # Worktree might not exist, which is fine
        if "is not a working tree" in e.stderr or "not found" in e.stderr:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "success": True,
                        "worktree_path": worktree_path,
                        "message": f"Worktree {worktree_path} does not exist (already cleaned up)"
                    }, indent=2)
                }]
            }

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Failed to remove worktree: {e.stderr}",
                    "worktree_path": worktree_path
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unexpected error: {str(e)}",
                    "worktree_path": worktree_path
                }, indent=2)
            }],
            "isError": True
        }



@tool(
    "create_schedule",
    "Analyze plan tasks and create a parallelizability execution schedule",
    {
        "type": "object",
        "properties": {
            "plan_branch": {
                "type": "string",
                "description": "Plan branch name, e.g. plan/session-20250101-120000"
            },
            "domain_context": {
                "type": "string",
                "description": "Optional background knowledge to help judge consensus risk. E.g. 'This is a math tutorial where notation consistency matters' or 'These are independent microservices with no shared schema'."
            }
        },
        "required": ["plan_branch"]
    }
)
async def create_schedule_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze plan tasks using token-cost inequality and generate an
    execution wave schedule.

    The schedule controls whether tasks run SERIAL or STAGED_PARALLEL.
    Pure computation, no LLM calls (~<100ms).

    Args:
        args: Dictionary with 'plan_branch' key and optional 'domain_context'

    Returns:
        MCP tool response with schedule JSON including waves, group_decisions,
        and risk_factors for orchestrator guidance.
    """
    plan_branch = args.get("plan_branch", "")
    domain_context = args.get("domain_context", "")
    if not plan_branch:
        return {
            "content": [{"type": "text", "text": json.dumps(
                {"error": "plan_branch is required"}, indent=2
            )}],
            "isError": True
        }

    try:
        # Read plan commit
        result = subprocess.run(
            ['git', 'log', plan_branch, '--format=%B', '-n', '1'],
            capture_output=True, text=True, encoding='utf-8',
            check=True, timeout=10
        )
        plan = parse_plan_commit(result.stdout)
        tasks = plan.get("tasks", [])
        if not tasks:
            return {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": "No tasks found in plan", "plan_branch": plan_branch},
                    indent=2
                )}],
                "isError": True
            }

        # Get max_parallel from SDKWorkerManager
        from .sdk_workers import get_sdk_worker_manager
        mgr = get_sdk_worker_manager()
        max_parallel = mgr.max_parallel if mgr else 6

        # Run scheduler (pure computation, no LLM)
        from .scheduler import ParallelizabilityScheduler
        convention_planning_tokens = plan.get("convention_planning_tokens", 0)
        # Per-layer alignment costs (fall back to the single value only if absent).
        layer_convention_tokens = plan.get("layer_convention_tokens", None)
        scheduler = ParallelizabilityScheduler(
            max_parallel=max_parallel,
            domain_context=domain_context,
        )
        waves, group_decisions = scheduler.create_schedule(
            tasks, convention_planning_tokens=convention_planning_tokens,
            layer_convention_tokens=layer_convention_tokens,
        )

        # Store schedule in SDKWorkerManager
        if mgr:
            wave_dicts = [w.to_dict() for w in waves]
            mgr.set_schedule(wave_dicts)

        # Build compact response — only what orchestrator needs to act on.
        # Full group_decisions/risk_factors bloat context (and fork
        # inherits it), so we emit a minimal summary instead.
        compact_schedule = []
        for w in waves:
            entry = {
                "wave": w.wave_index,
                "task_ids": w.task_ids,
                "strategy": w.strategy.value,
            }
            if w.conventions_prompt:
                entry["conventions_prompt"] = w.conventions_prompt
            compact_schedule.append(entry)

        # One-line decision summary per group (replaces verbose reasoning)
        decision_summary = [
            f"{d['group_id']}: {d['task_ids']} → {d['strategy']} (ratio={d['ratio']})"
            for d in group_decisions
        ]

        response = {
            "schedule": compact_schedule,
            "decisions": decision_summary,
            "n_waves": len(waves),
            "n_tasks": len(tasks),
        }

        return {
            "content": [{"type": "text", "text": json.dumps(response, indent=2)}]
        }

    except subprocess.CalledProcessError as e:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"Git command failed: {e.stderr}",
                "plan_branch": plan_branch
            }, indent=2)}],
            "isError": True
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"Schedule creation failed: {str(e)}",
                "plan_branch": plan_branch
            }, indent=2)}],
            "isError": True
        }


@tool(
    "launch_wave",
    "Batch-prepare AND launch an entire wave in one call. Creates branches, worktrees, metadata commits, and launches all workers. Set auto_launch=true and provide instructions per worker.",
    {
        "type": "object",
        "properties": {
            "plan_branch": {
                "type": "string",
                "description": "Plan branch name, e.g. plan/session-20250101-120000"
            },
            "session_id": {
                "type": "string",
                "description": "Session ID, e.g. session-20250101-120000"
            },
            "workers": {
                "type": "array",
                "description": "List of worker specs — one per task in the wave",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Task ID, e.g. '001'"},
                        "branch_slug": {"type": "string", "description": "Branch suffix, e.g. 'vectors'"},
                        "instructions": {"type": "string", "description": "Task-specific instructions for the worker (required when auto_launch=true)"}
                    },
                    "required": ["task_id", "branch_slug"]
                }
            },
            "auto_launch": {
                "type": "boolean",
                "description": "If true, launch workers immediately after git setup (default: true)",
                "default": True
            },
            "model": {
                "type": "string",
                "description": "Ignored when the session model is set; workers use the session model"
            }
        },
        "required": ["plan_branch", "session_id", "workers"]
    }
)
async def launch_wave(args: Dict[str, Any]) -> Dict[str, Any]:
    """Batch-prepare an entire wave: branches + worktrees + metadata commits.

    Pure git-setup tool — does NOT launch workers (that crosses MCP boundaries).
    The orchestrator calls launch_worker_async separately after this returns.

    Zero-checkout strategy (no HEAD switching):
      1. git branch <name> flow  (instant, no checkout)
      2. git worktree add        (independent checkout)
      3. git -C <worktree> commit (metadata + instruction files)

    Returns prepared specs for the orchestrator to launch workers.
    """
    plan_branch = args.get("plan_branch", "")
    session_id = args.get("session_id", "")
    worker_specs = args.get("workers", [])

    if not plan_branch or not session_id or not worker_specs:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": "plan_branch, session_id, and workers are required"
            }, indent=2)}],
            "isError": True
        }

    try:
        # Read plan to get task metadata
        result = subprocess.run(
            ['git', 'log', plan_branch, '--format=%B', '-n', '1'],
            capture_output=True, text=True, encoding='utf-8',
            check=True, timeout=10
        )
        plan = parse_plan_commit(result.stdout)
        plan_tasks = {t["id"]: t for t in plan.get("tasks", [])}
        plan_version = plan.get("plan_version", "v1")
        session_goal = plan.get("user_request", "")

        # Validate all task_ids exist in plan
        for spec in worker_specs:
            if spec["task_id"] not in plan_tasks:
                return {
                    "content": [{"type": "text", "text": json.dumps({
                        "error": f"Task {spec['task_id']} not found in plan",
                        "available_tasks": list(plan_tasks.keys())
                    }, indent=2)}],
                    "isError": True
                }

        completed_ids = [
            t["id"] for t in plan.get("tasks", [])
            if t.get("status") == "completed"
        ]

        # Instruction file sources
        prompts_dir = Path(__file__).parent / "prompts"
        instruction_files = [
            ("orchestrator.md", "ORCHESTRATOR_INSTRUCTIONS.md"),
            ("worker.md", "WORKER_INSTRUCTIONS.md"),
        ]
        conventions_path = Path.cwd() / "CONVENTIONS.md"

        # --- Phase 1: Create all branches (no checkout, instant) ---
        branches = []
        for spec in worker_specs:
            branch_name = f"task/{spec['task_id']}-{spec['branch_slug']}"
            check = subprocess.run(
                ['git', 'rev-parse', '--verify', branch_name],
                capture_output=True, timeout=5
            )
            if check.returncode != 0:
                subprocess.run(
                    ['git', 'branch', branch_name, 'flow'],
                    capture_output=True, check=True, timeout=5
                )
            branches.append(branch_name)

        # --- Phase 2: Create all worktrees ---
        list_result = subprocess.run(
            ['git', 'worktree', 'list'],
            capture_output=True, text=True, timeout=5
        )
        existing_worktrees = list_result.stdout

        worktree_paths = []
        for i, spec in enumerate(worker_specs):
            worker_id = str(i + 1)
            worktree_path = f".worktrees/worker-{worker_id}"

            if worktree_path in existing_worktrees:
                subprocess.run(
                    ['git', 'worktree', 'remove', '--force', worktree_path],
                    capture_output=True, text=True, timeout=5
                )

            subprocess.run(
                ['git', 'worktree', 'add', worktree_path, branches[i]],
                capture_output=True, text=True, check=True, timeout=10
            )
            worktree_paths.append(worktree_path)

        # --- Phase 3: Copy instruction files + commit metadata ---
        for i, spec in enumerate(worker_specs):
            tid = spec["task_id"]
            slug = spec["branch_slug"]
            task = plan_tasks[tid]
            worktree_path = worktree_paths[i]
            worktree_abs = str(Path.cwd() / worktree_path)

            wt_squid_dir = Path(worktree_abs) / ".squidagent"
            wt_squid_dir.mkdir(exist_ok=True)
            copied_files = []
            for source_file, dest_file in instruction_files:
                source_path = prompts_dir / source_file
                dest_path = wt_squid_dir / dest_file
                if source_path.exists():
                    shutil.copy2(source_path, dest_path)
                    copied_files.append(f".squidagent/{dest_file}")

            if conventions_path.exists():
                shutil.copy2(conventions_path, Path(worktree_abs) / "CONVENTIONS.md")
                copied_files.append("CONVENTIONS.md")

            if copied_files:
                subprocess.run(
                    ['git', '-C', worktree_abs, 'add'] + copied_files,
                    capture_output=True, check=True, timeout=10
                )

            preconditions = task.get("preconditions", [])
            provides = task.get("provides", [])
            files = task.get("files", [])
            depends_on = task.get("depends_on", [])
            enables = task.get("enables", [])
            description = task.get("description", "")
            estimated_time = task.get("estimated_time", "")
            priority = task.get("priority", "medium")

            pre_section = "Preconditions:\n" + "".join(f"  - {p}\n" for p in preconditions) if preconditions else "Preconditions: []\n"
            prov_section = "Provides:\n" + "".join(f"  - {p}\n" for p in provides) if provides else "Provides: []\n"
            files_section = "Files to modify:\n" + "".join(f"  - {f}\n" for f in files) if files else "Files to modify: []\n"
            parallel_with = [s["task_id"] for s in worker_specs if s["task_id"] != tid]

            commit_msg = f"""Initialize task/{tid}-{slug}

## Task Metadata
ID: {tid}
Description: {description}
Status: pending

## Dependencies
{pre_section}
{prov_section}

## Files
{files_section}

## Context
Session Goal: {session_goal}
Session ID: {session_id}
Plan Branch: {plan_branch}
Plan Version: {plan_version}
Depends on: {str(depends_on) if depends_on else '[]'}
Enables: {str(enables) if enables else '[]'}
Parallel with: {str(parallel_with)}
Completed Tasks: {str(completed_ids) if completed_ids else '[]'}

## Estimates
Estimated Time: {estimated_time}
Priority: {priority}
"""
            subprocess.run(
                ['git', '-C', worktree_abs, 'commit', '--allow-empty', '-m', commit_msg],
                capture_output=True, check=True, timeout=10
            )

        # Build prepared specs
        prepared = []
        for i, spec in enumerate(worker_specs):
            prepared.append({
                "worker_id": str(i + 1),
                "task_id": spec["task_id"],
                "task_branch": branches[i],
                "worktree": worktree_paths[i],
            })

        # --- Phase 4: Auto-launch workers (default: true) ---
        auto_launch = args.get("auto_launch", True)
        launch_results = []
        if auto_launch:
            try:
                from .sdk_workers import get_sdk_worker_manager, _run_sdk_worker_task
                import asyncio
                import logging

                manager = get_sdk_worker_manager()
                # Use session-configured model, fall back to args then default
                from . import DEFAULT_MODEL
                model = getattr(manager, '_model', None) or args.get("model") or DEFAULT_MODEL

                for i, spec in enumerate(worker_specs):
                    worker_id = str(i + 1)
                    task_branch_name = branches[i]
                    cwd_path = worktree_paths[i]
                    instructions = spec.get("instructions", "")

                    if not instructions:
                        launch_results.append({"worker_id": worker_id, "error": "missing instructions"})
                        continue

                    session_info = {
                        'session_id': session_id,
                        'plan_branch': plan_branch,
                        'model': model,
                    }

                    sched_ok, sched_reason = manager.check_schedule_allows(task_branch_name)
                    if not sched_ok:
                        launch_results.append({"worker_id": worker_id, "error": sched_reason})
                        continue

                    ok, err = manager._validate_worker_params(
                        worker_id, task_branch_name, session_info, cwd_path, instructions)
                    if not ok:
                        launch_results.append({"worker_id": worker_id, "error": err})
                        continue

                    _mgr = manager
                    _wid = worker_id
                    _tb = task_branch_name
                    _si = session_info
                    _cwd = cwd_path
                    _ins = instructions

                    async def _safe_wrapper(m=_mgr, w=_wid, t=_tb, s=_si, c=_cwd, i=_ins):
                        try:
                            await _run_sdk_worker_task(m, w, t, s, c, i)
                        except Exception as e:
                            import traceback
                            err_msg = f"Worker-{w} error: {e}\n{traceback.format_exc()}"
                            logging.getLogger("squidagent.orchestrator").error(err_msg)
                            print(f"  [WORKER ERROR] {err_msg}", flush=True)
                            # Inject error completion so orchestrator knows
                            if m.control_queue:
                                await m._inject_completion_event(w, t, 1, 0, str(e))

                    task = asyncio.create_task(_safe_wrapper())
                    launch_results.append({"worker_id": worker_id, "launched": True, "_task": task})

                # Wait briefly for workers to register in active_workers
                if launch_results:
                    await asyncio.sleep(0.5)
                    for r in launch_results:
                        t = r.pop("_task", None)
                        wid = r.get("worker_id")
                        if t and wid and wid in manager.active_workers:
                            manager.active_workers[wid]['task'] = t
            except Exception as e:
                launch_results.append({"error": f"auto_launch failed: {str(e)}"})

        result = {
            "success": True,
            "prepared": prepared,
            "n_workers": len(prepared),
        }
        if auto_launch:
            n_launched = sum(1 for r in launch_results if r.get("launched"))
            result["launched"] = n_launched
            result["message"] = f"Git setup complete. {n_launched}/{len(prepared)} workers launched."
            if any(r.get("error") for r in launch_results):
                result["launch_errors"] = [r for r in launch_results if r.get("error")]
        else:
            result["message"] = "All branches, worktrees, and metadata ready. Call launch_worker_async for each worker now."

        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
        }

    except subprocess.CalledProcessError as e:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"Git command failed: {e.stderr.decode() if e.stderr else str(e)}",
            }, indent=2)}],
            "isError": True
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"launch_wave failed: {str(e)}",
            }, indent=2)}],
            "isError": True
        }


@tool(
    "finalize_wave",
    "Batch merge + cleanup + plan update for a completed wave. Optionally launch the next wave immediately via next_wave parameter.",
    {
        "type": "object",
        "properties": {
            "plan_branch": {
                "type": "string",
                "description": "Plan branch name (e.g., 'plan/session-20250117-120000')"
            },
            "session_id": {
                "type": "string",
                "description": "Session ID (e.g., 'session-20250117-120000'), required if next_wave is provided"
            },
            "completed_tasks": {
                "type": "array",
                "description": "List of completed tasks to merge and clean up",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Task ID (e.g., '001')"},
                        "task_branch": {"type": "string", "description": "Task branch name (e.g., 'task/001-vectors')"},
                        "worker_id": {"type": "string", "description": "Worker ID (e.g., '1')"},
                        "description": {"type": "string", "description": "Short description for merge commit"}
                    },
                    "required": ["task_id", "task_branch", "worker_id"]
                }
            },
            "next_wave": {
                "type": "object",
                "description": "Optional: prepare and launch next wave immediately after finalize",
                "properties": {
                    "workers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task_id": {"type": "string"},
                                "branch_slug": {"type": "string"},
                                "instructions": {"type": "string"}
                            },
                            "required": ["task_id", "branch_slug", "instructions"]
                        }
                    },
                    "model": {"type": "string", "description": "Ignored when the session model is set"}
                }
            }
        },
        "required": ["plan_branch", "completed_tasks"]
    }
)
async def finalize_wave(args: Dict[str, Any]) -> Dict[str, Any]:
    """Batch finalize a completed wave: merge all task branches, remove worktrees, update plan.

    This is the counterpart to launch_wave — one call to clean up everything that
    launch_wave set up. Replaces N×(parse_worker_commit + merge + remove_worktree +
    update_plan_branch) with a single tool call.

    Steps:
        1. Ensure on flow branch
        2. Merge each task branch into flow (sequential, --no-ff)
        3. Remove all worktrees
        4. Update plan branch (mark all tasks complete)
    """
    plan_branch = args.get("plan_branch", "")
    completed_tasks = args.get("completed_tasks", [])

    if not plan_branch or not completed_tasks:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": "plan_branch and completed_tasks are required"
            }, indent=2)}],
            "isError": True
        }

    results = {"merged": [], "cleaned": [], "plan_updated": False, "errors": []}

    try:
        # 1. Ensure on flow branch
        subprocess.run(
            ['git', 'checkout', 'flow'],
            capture_output=True, text=True, timeout=10, check=True
        )

        # 2. Merge each task branch
        for task in completed_tasks:
            branch = task["task_branch"]
            desc = task.get("description", branch.split("/")[-1] if "/" in branch else branch)
            merge_msg = f"Merge {branch}: {desc}"
            merge_result = subprocess.run(
                ['git', 'merge', branch, '--no-ff', '-m', merge_msg],
                capture_output=True, text=True, timeout=30
            )
            if merge_result.returncode == 0:
                results["merged"].append(branch)
            else:
                results["errors"].append(f"Merge {branch}: {merge_result.stderr.strip()}")

        # 3. Remove worktrees
        for task in completed_tasks:
            wid = task["worker_id"]
            worktree_path = f".worktrees/worker-{wid}"
            rm_result = subprocess.run(
                ['git', 'worktree', 'remove', '--force', worktree_path],
                capture_output=True, text=True, timeout=10
            )
            if rm_result.returncode == 0 or "not a working tree" in rm_result.stderr:
                results["cleaned"].append(worktree_path)

        # 4. Update plan branch — mark all tasks complete
        task_ids = [t["task_id"] for t in completed_tasks]
        try:
            # Save current branch
            cur = subprocess.run(
                ['git', 'branch', '--show-current'],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()

            subprocess.run(
                ['git', 'checkout', plan_branch],
                capture_output=True, text=True, timeout=10, check=True
            )

            # Read current plan
            plan_msg = subprocess.run(
                ['git', 'log', '-n', '1', '--format=%B'],
                capture_output=True, text=True, timeout=10
            ).stdout.strip()

            # Mark tasks complete in plan text
            updated_plan = plan_msg
            for tid in task_ids:
                updated_plan = updated_plan.replace(
                    f"Status: pending",
                    f"Status: completed",
                    1  # Replace one at a time (each task has its own Status line)
                )

            # Get version number
            commit_count = subprocess.run(
                ['git', 'rev-list', '--count', 'HEAD'],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()
            new_version = f"v{int(commit_count) + 1}"
            updated_plan = updated_plan.replace(
                updated_plan.split('\n')[0],
                f"Initialize execution plan {new_version}"
            )

            subprocess.run(
                ['git', 'commit', '--allow-empty', '-m', updated_plan],
                capture_output=True, text=True, timeout=10, check=True
            )

            # Return to flow
            subprocess.run(
                ['git', 'checkout', cur or 'flow'],
                capture_output=True, text=True, timeout=10
            )
            results["plan_updated"] = True
            results["plan_version"] = new_version
            results["completed_task_ids"] = task_ids

        except Exception as e:
            results["errors"].append(f"Plan update: {str(e)}")
            # Try to return to flow
            subprocess.run(
                ['git', 'checkout', 'flow'],
                capture_output=True, text=True, timeout=10
            )

        # 5. Launch next wave if provided
        next_wave_spec = args.get("next_wave")
        if next_wave_spec and next_wave_spec.get("workers"):
            try:
                next_args = {
                    "plan_branch": plan_branch,
                    "session_id": args.get("session_id", ""),
                    "workers": next_wave_spec["workers"],
                    "auto_launch": True,
                    "model": next_wave_spec.get("model"),
                }
                next_result = await launch_wave.handler(next_args)
                next_data = json.loads(next_result["content"][0]["text"])
                results["next_wave"] = next_data
            except Exception as e:
                results["errors"].append(f"next_wave launch: {str(e)}")

        return {
            "content": [{"type": "text", "text": json.dumps({
                "success": len(results["errors"]) == 0,
                **results
            }, indent=2)}]
        }

    except Exception as e:
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"finalize_wave failed: {str(e)}",
                **results
            }, indent=2)}],
            "isError": True
        }


def create_git_tools_server():
    """Create MCP server with git parsing tools.

    Returns:
        MCP server instance with git tools

    Usage:
        In CLI setup:
            options = ClaudeAgentOptions(
                mcp_servers={"git": create_git_tools_server()},
                ...
            )

        Agents can then use:
            - mcp__git__parse_task: Parse task metadata from branch commit
            - mcp__git__parse_plan: Parse plan data from plan branch commit (commit-only)
            - mcp__git__get_provides: Query completed task capabilities from flow
            - mcp__git__parse_worker_commit: Parse worker's latest commit (design + TODO progress)
            - mcp__git__create_plan_branch: Create plan branch with instruction files and metadata
            - mcp__git__create_task_branch: Create task branch with instruction files and metadata
            - mcp__git__update_plan_branch: Update plan with completed tasks and newly discovered tasks
            - mcp__git__create_worktree: Create isolated git worktree for parallel worker execution
            - mcp__git__remove_worktree: Remove git worktree after worker completes
            - mcp__git__create_schedule: Analyze tasks and create parallelizability schedule
    """
    tools = [
        parse_task,
        parse_plan,
        get_provides,
        parse_worker_commit_tool,
        create_plan_branch,
        create_task_branch,
        update_plan_branch,
        create_worktree,
        remove_worktree,
        launch_wave,
        finalize_wave,
        create_schedule_tool,
    ]
    return create_sdk_mcp_server(
        name="git",
        version="1.0.0",
        tools=tools,
    )
