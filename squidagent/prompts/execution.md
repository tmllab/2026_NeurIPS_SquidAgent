### Execution Mode: Git Worktree (SDK Workers)

**After `create_plan_branch` returns, inspect the `schedule` field:**

- `"staged_parallel"` or `"full_parallel"` → call `launch_wave` for these tasks
- **`"serial"` → you MUST write these tasks DIRECTLY YOURSELF using Write/Edit. Do NOT launch workers for serial tasks.**

**Execution order**: First call `launch_wave` for parallel tasks, then while workers run, write serial tasks yourself (or after, depending on dependencies); follow the conventions.

**One-Call Batch Launch — call `launch_wave` with per-worker `instructions`:**

```python
result = mcp__git__launch_wave({
    "plan_branch": "plan/session-...",
    "session_id": "session-...",
    "auto_launch": true,
    "model": "opus",
    "workers": [
        {"task_id": "001", "branch_slug": "vectors", "instructions": "Write vectors.md covering... Follow these conventions: <full conventions>"},
        {"task_id": "002", "branch_slug": "matrices", "instructions": "Write matrix_ops.md covering... Follow these conventions: <full conventions>"},
    ]
})
```

**For multi-wave DAGs**, use `finalize_wave` with `next_wave` to chain waves:

```python
result = mcp__git__finalize_wave({
    "plan_branch": "plan/session-...",
    "session_id": "session-...",
    "completed_tasks": [
        {"task_id": "001", "task_branch": "task/001-vectors", "worker_id": "1"},
    ],
    "next_wave": {
        "model": "opus",
        "workers": [
            {"task_id": "003", "branch_slug": "eigen", "instructions": "Write eigen.md... Follow these conventions: <full conventions>"},
        ]
    }
})
```

**CRITICAL rules:**
- Each worker writes ONLY its assigned file(s) — no overlap
- Include full conventions in each worker prompt (workers share your context but be explicit)
- SERIAL tasks: write directly yourself, do NOT spawn workers
- After all workers complete: review for cross-file consistency and fix any issues
- Do NOT spawn a separate worker for review/verification — do it yourself directly

**For multi-layer DAGs:**
1. Layer 0 tasks first (spawn workers or write serial)
2. Wait for all Layer 0 workers to complete
3. Layer 1 tasks next (can reference Layer 0 outputs)
4. Repeat until all layers done
