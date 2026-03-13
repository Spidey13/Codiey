"""CLI entry point: `codiey start`."""

import os
import sys
import webbrowser
import time

import click
from dotenv import load_dotenv


@click.group()
def main():
    """Codiey — Voice-first codebase thinking partner."""
    pass


@main.command()
@click.option("--port", default=7842, help="Port to run on.")
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser.")
@click.option(
    "--workspace",
    default=None,
    help="Path to workspace. Defaults to current directory.",
)
def start(port: int, no_browser: bool, workspace: str | None):
    """Start a Codiey thinking session."""

    load_dotenv()

    # Validate API key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        click.echo(
            click.style("✗ ", fg="red")
            + "GEMINI_API_KEY not found.\n"
            + "  Set it in a .env file or as an environment variable.\n"
            + "  Get a free key at: https://aistudio.google.com/apikey"
        )
        sys.exit(1)

    # Resolve workspace
    workspace_path = os.path.abspath(workspace or os.getcwd())
    if not os.path.isdir(workspace_path):
        click.echo(click.style("✗ ", fg="red") + f"Not a directory: {workspace_path}")
        sys.exit(1)

    click.echo(
        click.style("╔══════════════════════════════════════════╗", fg="cyan")
    )
    click.echo(
        click.style("║          ", fg="cyan")
        + click.style("Codiey", fg="cyan", bold=True)
        + click.style(" 🎙️                       ║", fg="cyan")
    )
    click.echo(
        click.style("╚══════════════════════════════════════════╝", fg="cyan")
    )
    click.echo(f"  Workspace: {workspace_path}")
    click.echo(f"  Server:    http://localhost:{port}")
    click.echo()

    # Set workspace path for the app to read
    os.environ["CODIEY_WORKSPACE"] = workspace_path

    # Auto-open browser (with slight delay so server can start)
    if not no_browser:

        def open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}")

        import threading

        threading.Thread(target=open_browser, daemon=True).start()

    # Start uvicorn
    import uvicorn

    uvicorn.run(
        "codiey.app:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
