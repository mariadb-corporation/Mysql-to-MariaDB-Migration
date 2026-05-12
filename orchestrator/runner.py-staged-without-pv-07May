from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Tuple, Any, Optional

def run_step(
    repo_root: Path,
    script: str,
    args: Optional[List[str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Run a single shell script as a subprocess.

    - repo_root: run from repo root (so relative paths work)
    - script: relative path like scripts/00_precheck.sh
    - args: list of args (strings)
    - extra_env: injected env vars
    """
    args = args or []
    extra_env = extra_env or {}
    script_path = (repo_root / script).resolve()
    if not script_path.exists():
        return False, {"error": "script_not_found", "script": script, "path": str(script_path)}

    # Ensure executable (best-effort)
    try:
        script_path.chmod(script_path.stat().st_mode | 0o111)
    except Exception:
        pass

    env = os.environ.copy()
    env.update(extra_env)

    cmd = [str(script_path)] + args

    if log:
        log("CMD " + " ".join(shlex.quote(c) for c in cmd))

    p = subprocess.Popen(
        cmd,
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    out_lines: List[str] = []
    assert p.stdout is not None
    for line in p.stdout:
        out_lines.append(line.rstrip("\n"))
        if log:
            log("OUT " + line.rstrip("\n"))

    rc = p.wait()
    meta = {
        "script": script,
        "args": args,
        "returncode": rc,
        "output_tail": out_lines[-50:],  # keep last 50 lines for report
    }
    return (rc == 0), meta
