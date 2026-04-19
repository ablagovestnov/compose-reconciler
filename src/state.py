"""Hashing, status.json writes, change detection."""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TRACKED_SUBDIRS = ("site", "backend", "bot")
COMPOSE_NAMES = ("docker-compose.yml", "compose.yml", "compose.yaml", "docker-compose.yaml")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_dir(d: Path) -> str:
    if not d.is_dir():
        return ""
    h = hashlib.sha256()
    for f in sorted(d.rglob("*")):
        if not f.is_file():
            continue
        if "/.reconciler/" in f.as_posix() or f.name in ("DEPLOY", "REMOVE"):
            continue
        rel = str(f.relative_to(d))
        h.update(rel.encode())
        h.update(b":")
        h.update(_sha256_file(f).encode())
        h.update(b"\n")
    return h.hexdigest()


def compute_hashes(project_dir: Path) -> dict:
    out: dict = {}
    for sub in TRACKED_SUBDIRS:
        out[sub] = _sha256_dir(project_dir / sub)
    out["compose"] = ""
    for name in COMPOSE_NAMES:
        f = project_dir / name
        if f.is_file():
            out["compose"] = _sha256_file(f)
            break
    env_f = project_dir / ".env"
    out["env"] = _sha256_file(env_f) if env_f.is_file() else ""
    return out


def diff_action(current: dict, applied: dict) -> str | None:
    """Minimal rebuild hint based on which subdirs changed. None = no change."""
    if not applied:
        return None
    changed = {k for k in current if current.get(k) != applied.get(k)}
    if not changed:
        return None
    if "compose" in changed or "env" in changed:
        return "full"
    if "backend" in changed:
        return "backend"
    if "bot" in changed:
        return "bot"
    if "site" in changed:
        return "site"
    return "full"


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(exist_ok=True, parents=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_status(project_dir: Path, state: str, containers: list,
                 last_action: str, last_error: str | None) -> None:
    payload = {
        "slug": project_dir.name,
        "state": state,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "last_action": last_action,
        "containers": containers,
        "last_error": last_error,
    }
    _atomic_write(project_dir / ".reconciler" / "status.json",
                  json.dumps(payload, indent=2) + "\n")


def read_status(project_dir: Path) -> dict | None:
    f = project_dir / ".reconciler" / "status.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_applied_hashes(project_dir: Path) -> dict:
    f = project_dir / ".reconciler" / "applied.hash"
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_applied_hashes(project_dir: Path, hashes: dict) -> None:
    _atomic_write(project_dir / ".reconciler" / "applied.hash",
                  json.dumps(hashes, indent=2) + "\n")


def write_log(project_dir: Path, content: str) -> None:
    _atomic_write(project_dir / ".reconciler" / "last_apply.log", content)
