"""Zipapp entry point.

Phase scripts cannot be exec'd from inside the archive, so they are unpacked
once into ~/.local/share/<app>/<version>/ and run from there. That path is
user-owned and not mounted noexec, unlike /tmp on hardened RHEL builds.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time
import zipfile

APP_NAME = "mariadb-migrator"
PAYLOAD_PREFIX = "_payload/"
ENV_VAR = "MARIADB_MIGRATOR_ROOT"
# Payload dirs idle this long are pruned when a new version installs.
KEEP_UNUSED_HOURS = 24


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


def _stamp(archive):
    """Content hash of the archive, so a rebuilt bundle re-extracts."""
    h = hashlib.sha256()
    with open(archive, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prune(current, keep_hours=KEEP_UNUSED_HOURS):
    """Drop payload dirs for other versions. Never fatal."""
    parent = os.path.dirname(current)
    cutoff = time.time() - keep_hours * 3600
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        path = os.path.join(parent, name)
        if path == current or not os.path.isdir(path):
            continue
        # Only touch dirs we wrote: they carry a completion marker.
        if not os.path.exists(os.path.join(path, ".complete")):
            continue
        try:
            if keep_hours and os.path.getmtime(path) > cutoff:
                continue
            shutil.rmtree(path)
        except OSError as exc:
            sys.stderr.write("WARNING: could not remove %s: %s\n" % (path, exc))


def _extract(archive, dest):
    """Unpack the payload into dest atomically. Safe to race."""
    marker = os.path.join(dest, ".complete")
    stamp = _stamp(archive)
    if os.path.exists(marker):
        try:
            if open(marker).read().strip() == stamp:
                return dest
        except OSError:
            pass
        # Same version, different build: replace the payload.
        stale = "%s.stale-%d" % (dest, os.getpid())
        try:
            os.rename(dest, stale)
            shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            shutil.rmtree(dest, ignore_errors=True)

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

        with open(os.path.join(staging, ".complete"), "w") as fh:
            fh.write(stamp + "\n")
        os.rename(staging, dest)
        _prune(dest)
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
