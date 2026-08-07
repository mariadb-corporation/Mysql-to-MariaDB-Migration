import sys
from importlib.metadata import version
from pathlib import Path

if not sys.stdout.isatty():
    sys.exit(
        "requires an interactive terminal; use "
        "'python3 -m orchestrator.migrationctl' for non-interactive runs"
    )

if tuple(int(p) for p in version("rich").split(".")[:2]) < (14, 2):
    sys.exit("textual requires rich>=14.2.0; run: pip install -r orchestrator/requirements.txt")

from orchestrator.tui.app import MigrationApp

_repo_root = Path(__file__).resolve().parents[2]
MigrationApp(repo_root=_repo_root).run()
