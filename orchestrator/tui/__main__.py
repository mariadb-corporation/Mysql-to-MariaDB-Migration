import sys
from importlib.metadata import version

if not sys.stdout.isatty():
    sys.exit(
        "requires an interactive terminal; use "
        "'python3 -m orchestrator.migrationctl' for non-interactive runs"
    )

if tuple(int(p) for p in version("rich").split(".")[:2]) < (14, 2):
    sys.exit("textual requires rich>=14.2.0; run: pip install -r orchestrator/requirements.txt")
