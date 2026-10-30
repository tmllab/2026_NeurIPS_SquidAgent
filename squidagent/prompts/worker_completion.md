**Per-worker completion:** When a worker completes, respond with ONLY a short sentence like "Worker-1 done, waiting for 2,3,4,5." Do NOT output tables, status summaries, or progress reports. Do NOT merge, cleanup, or update plan yet. Wait for all workers in the current wave to finish.

**After ALL workers in a wave complete:** Call `mcp__git__finalize_wave` ONCE to batch-merge all task branches, remove all worktrees, and update the plan in a single operation:

```python
mcp__git__finalize_wave({
    "plan_branch": "plan/session-...",
    "completed_tasks": [
        {"task_id": "001", "task_branch": "task/001-vectors", "worker_id": "1", "description": "Create vectors chapter"},
        {"task_id": "002", "task_branch": "task/002-matrices", "worker_id": "2", "description": "Create matrices chapter"},
        # ... one entry per completed worker in this wave
    ]
})
```

This replaces per-worker `parse_worker_commit` + `git merge` + `remove_worktree` + `update_plan_branch` (4N tool calls) with 1 call.

**After finalize_wave returns**, do ONE quick cross-file consistency review: use a single Grep to spot-check notation/terminology across all output files. Fix any issues directly with Edit. Do NOT launch separate Agent calls per file — that costs more time than the review saves. Then launch the next wave if applicable.