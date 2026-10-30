"""
SquidAgent CLI Entry Point

Interactive Textual UI for SquidAgent development sessions.
Just type `squidagent` to start an interactive session.
"""

import sys
import click

from squidagent import DEFAULT_MAX_PARALLEL, DEFAULT_MODE, DEFAULT_MODEL


@click.command()
@click.option('--model', default=DEFAULT_MODEL, show_default=True,
              help='Claude model ID for the orchestrator and all workers')
@click.option('--max-parallel', type=click.IntRange(min=1), default=DEFAULT_MAX_PARALLEL,
              show_default=True, help='Maximum number of parallel workers (worktree mode)')
@click.option('--verbose', is_flag=True,
              help='Enable verbose output')
@click.option('--debug', is_flag=True,
              help='Enable debug mode')
@click.option('--single-shot', is_flag=True,
              help='Exit automatically after the first request completes (no follow-up prompt)')
@click.option('--effort', type=click.Choice(['low', 'medium', 'high', 'max']), default=None,
              help='Thinking effort level (overrides user settings.json)')
@click.option('--worktree', is_flag=True,
              help='Worktree mode: SDK workers in git worktrees that fork the orchestrator '
                   'session (default is agent-tool mode, which is cheaper)')
def main(model, max_parallel, verbose, debug, single_shot, effort, worktree):
    """
    SquidAgent Interactive CLI

    Launch an interactive development session where you can:
    - Enter development requests
    - See real-time execution progress
    - Press ctrl+c to quit, ESC to interrupt, /help for commands
    """
    try:
        from squidagent.ui import SquidAgentApp
        from squidagent.setup_ui import run_setup_ui
        from squidagent.cli import setup_instruction_files

        # Run setup UI first (flow branch + CLAUDE.md)
        # Note: CLAUDE.md generation happens inside the UI with progress display
        try:
            setup_results = run_setup_ui()
            # Print summary of what was set up
            if setup_results.get("flow_branch_created"):
                base_branch = setup_results.get('base_branch')
                print(f"\n  ✓ Created 'flow' branch from '{base_branch}'")
            if setup_results.get("claude_md_generated"):
                print("  ✓ CLAUDE.md created and committed to flow branch")

            # Setup instruction files after flow branch is ready
            created_files = setup_instruction_files(debug=debug, max_parallel=max_parallel)
            if created_files:
                print("  ✓ Instruction files created in .squidagent/")

            if setup_results.get("flow_branch_created") or setup_results.get("claude_md_generated") or created_files:
                print()  # Add spacing before main UI only if setup happened
        except Exception as e:
            # If setup UI fails, continue with main UI anyway
            print(f"\n  Warning: Setup UI failed: {e}\n")
            if debug:
                import traceback
                traceback.print_exc()

        # Launch main app
        app = SquidAgentApp(
            model=model,
            max_parallel=max_parallel,
            verbose=verbose,
            debug=debug,
            single_shot=single_shot,
            effort=effort,
            mode="worktree" if worktree else DEFAULT_MODE,
        )
        try:
            app.run()
        except KeyboardInterrupt:
            print("\n\nExiting...")
    except ImportError as e:
        print(f"ERROR: Textual UI not available: {e}", file=sys.stderr)
        print("Install textual with: pip install textual", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
