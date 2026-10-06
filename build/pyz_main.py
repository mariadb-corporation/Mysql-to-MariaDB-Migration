"""Zipapp entry point.

Phase scripts cannot be exec'd from inside the archive, so they are unpacked
once into ~/.local/share/<app>/<version>/ and run from there. That path is
user-owned and not mounted noexec, unlike /tmp on hardened RHEL builds.
"""
import os
import shutil
import sys
import tempfile
import zipfile

APP_NAME = "mariadb-migrator"
PAYLOAD_PREFIX = "_payload/"
ENV_VAR = "MARIADB_MIGRATOR_ROOT"


def _archive_path():
    # Inside a zipapp, __file__ is <archive>/__main__.py.
    path = os.path.dirname(os.path.abspath(__file__))
    if zipfile.is_zipfile(path):
        return path
    # Running from an unpacked build dir (dev loop), not an archive.
    return None


def _version(archive=None):
    if archive is not None:
        with zipfile.ZipFile(archive) as zf:
            return zf.read("_version.txt").decode().strip()
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "_version.txt")) as fh:
        return fh.read().strip()


def _share_dir(version):
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME, version)


def _extract(archive, dest):
    """Unpack the payload into dest atomically. Safe to race."""
    marker = os.path.join(dest, ".complete")
    if os.path.exists(marker):
        return dest

    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".tmp-", dir=parent)
    try:
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if not name.startswith(PAYLOAD_PREFIX) or name.endswith("/"):
                    continue
                rel = name[len(PAYLOAD_PREFIX):]
                target = os.path.join(staging, rel)
                # Guard against traversal in a crafted archive.
                if not os.path.realpath(target).startswith(os.path.realpath(staging)):
                    raise RuntimeError("payload escapes staging dir: %s" % name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if rel.endswith(".sh"):
                    os.chmod(target, 0o755)

        open(os.path.join(staging, ".complete"), "w").close()
        os.rename(staging, dest)
    except OSError:
        # Lost the race; another process finished first.
        shutil.rmtree(staging, ignore_errors=True)
        if not os.path.exists(marker):
            raise
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


SUBCOMMANDS = ("assess", "plan", "run", "resume")


def _exec_launcher(root, archive):
    """Hand off to the interactive bash launcher.

    The launcher calls back into this same archive for assess/plan/run/resume
    via MIGCTL, so one artifact serves both the menu and the direct commands.
    """
    launcher = os.path.join(root, "mariadb-migrator")
    if not os.path.exists(launcher):
        sys.exit("ERROR: launcher missing from bundle: %s" % launcher)

    env = dict(os.environ)
    env[ENV_VAR] = root
    env.setdefault("REPO_ROOT", root)
    env["MIGCTL"] = os.path.abspath(archive) if archive else sys.executable

    os.execve("/bin/bash", ["bash", launcher] + sys.argv[1:], env)


def main():
    archive = _archive_path()
    if archive is not None:
        root = _extract(archive, _share_dir(_version(archive)))
    else:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_payload")

    argv = sys.argv[1:]
    if not argv or argv[0] not in SUBCOMMANDS:
        _exec_launcher(root, archive)  # does not return

    # Let the orchestrator find the phase scripts without __file__ math.
    os.environ.setdefault(ENV_VAR, root)

    from orchestrator import migrationctl
    return migrationctl.app()


if __name__ == "__main__":
    sys.exit(main())
