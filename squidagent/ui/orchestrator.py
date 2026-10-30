"""Orchestrator session management for SquidAgent UI."""

import asyncio

from textual.widgets import RichLog


class OrchestratorRunner:
    """Manages orchestrator session execution."""

    def __init__(self, app):
        """Initialize orchestrator runner.

        Args:
            app: The SquidAgentApp app instance
        """
        self.app = app

    async def run_orchestrator(self, request: str):
        """Run the orchestrator session (runs in background).

        Args:
            request: The initial development request
        """
        if getattr(self.app, 'mode', 'worktree') == 'agent-tool':
            await self._run_agent_tool()
            return

        from squidagent.cli import run_development_session
        from squidagent.sdk_workers import get_sdk_worker_manager

        # Initialize SDK-based worker manager
        sdk_worker_manager = get_sdk_worker_manager(
            control_queue=self.app.control_queue,
            debug=self.app.debug_mode,
            log_func=self._create_worker_log_func(),
            max_parallel=self.app.max_parallel
        )

        # Determine execution mode
        enable_parallel = self.app.max_parallel > 1
        num_workers = self.app.max_parallel if enable_parallel else 1

        try:
            await run_development_session(
                initial_request=request,
                session_id=self.app.session_id,
                model=self.app.model,
                permission_mode="bypassPermissions",
                enable_parallel=enable_parallel,
                max_parallel=self.app.max_parallel,
                verbose=self.app.verbose_mode,
                debug=self.app.debug_mode,
                num_workers=num_workers,
                control_queue=self.app.control_queue,
                logger=self.app.logger,
                auto_mode=self.app.auto_mode,
                resume_session_id=self.app.orchestrator_session_id,
                single_shot=getattr(self.app, 'single_shot', False),
                effort=getattr(self.app, 'effort', None),
            )

            # Capture session ID after session completes
            from squidagent import cli
            if hasattr(cli, '_current_session_id') and cli._current_session_id:
                self.app.orchestrator_session_id = cli._current_session_id

            # Session completed
            if getattr(self.app, 'single_shot', False):
                self.app._log("\n[dim]--single-shot: session complete, exiting.[/dim]\n")
                self.app.exit()
            else:
                self.app._log("\n[dim]Session ready for next request...[/dim]\n")

        except asyncio.CancelledError:
            log = self.app.query_one(RichLog)
            self.app._log("[yellow]Session cancelled[/yellow]")
        except Exception as e:
            self.app.logger.exception(f"Orchestrator error: {e}")
            log = self.app.query_one(RichLog)
            self.app._log(f"[.error]ERROR: {e}[/.error]")

    async def _run_agent_tool(self):
        """Agent-tool mode: one plan → execute session per request from the queue."""
        from squidagent.agent_tool import run_agent_tool_loop

        try:
            await run_agent_tool_loop(
                control_queue=self.app.control_queue,
                session_id=self.app.session_id,
                settings=lambda: (self.app.model, getattr(self.app, 'effort', None),
                                  self.app.max_parallel),
                single_shot=getattr(self.app, 'single_shot', False),
                logger=self.app.logger,
            )
            if getattr(self.app, 'single_shot', False):
                self.app._log("\n[dim]--single-shot: session complete, exiting.[/dim]\n")
                self.app.exit()
        except asyncio.CancelledError:
            self.app._log("[yellow]Session cancelled[/yellow]")
        except Exception as e:
            self.app.logger.exception(f"Agent-tool session error: {e}")
            self.app._log(f"[.error]ERROR: {e}[/.error]")

    def _create_worker_log_func(self):
        """Create a logging function that writes to the UI log AND file log.

        Returns:
            Callable that logs worker messages
        """
        def worker_log(msg: str):
            from rich.markup import escape
            log = self.app.query_one(RichLog)
            # Escape Rich markup in the message to prevent MarkupError
            # (paths like /Users/.../worker-1 contain [/...] which Rich
            # interprets as closing tags)
            safe_msg = escape(msg)
            # Always show worker output (it's important for tracking progress)
            # Use different styling for different message types
            if "[WORKER-" in msg and "ERROR]" in msg:
                # Error messages from workers
                self.app._log(f"[red]{safe_msg}[/red]")
            elif "[WORKER-" in msg and "]" in msg:
                # Normal output from workers
                self.app._log(f"[cyan]{safe_msg}[/cyan]")
            elif "[WorkerManager]" in msg and self.app.debug_mode:
                # Manager debug messages only in debug mode
                self.app._log(f"[dim]{safe_msg}[/dim]")
            elif "[SDKWorkerManager]" in msg and self.app.debug_mode:
                # SDK Manager debug messages only in debug mode
                self.app._log(f"[dim]{safe_msg}[/dim]")
            elif "[WORKER" in msg:
                # Other worker-related messages
                self.app._log(f"[blue]{safe_msg}[/blue]")

            # Also record to file log (important for debugging and tracking)
            if self.app.logger:
                # Remove color markup, record plain text to file
                clean_msg = msg  # Already plain text
                if "ERROR" in msg:
                    self.app.logger.error(clean_msg)
                elif "WARNING]" in msg or "WARN]" in msg:
                    self.app.logger.warning(clean_msg)
                elif self.app.debug_mode:
                    self.app.logger.debug(clean_msg)
                elif "[SDKWorkerManager]" not in msg:
                    self.app.logger.info(clean_msg)

        return worker_log
