"""compose-reconciler: watches /projects/{slug}/ and applies compose stacks safely.

Contract:
- Input (artifact producer writes): compose.yml, .env, tracked subdirs, DEPLOY or REMOVE sentinel.
- Output (reconciler writes): .reconciler/{status.json, applied.hash, last_apply.log}.

Single worker (FIFO). No HTTP. Policy rules enforced by linter.py before every apply.
"""
import logging
import os
import queue
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from linter import validate_compose
from policy import load_policy
from runner import compose_down, compose_ps, compose_restart, compose_up
from state import (
    compute_hashes,
    diff_action,
    read_applied_hashes,
    read_status,
    write_applied_hashes,
    write_log,
    write_status,
)

PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/projects"))
_host_dir_env = os.environ.get("HOST_PROJECTS_DIR", "").strip()
HOST_PROJECTS_DIR = Path(_host_dir_env) if _host_dir_env else None
POLICY_FILE = Path(os.environ.get("POLICY_FILE", "/etc/reconciler/policy.yaml"))
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "30"))
DEBOUNCE_SECONDS = 2.0

# Any directory whose name starts with `_` or `.` is treated as non-project
# (template, archive, internal bookkeeping). Producers may keep multiple
# template variants side-by-side under names like `_template-static`,
# `_template-fullstack`, etc — all are skipped.
COMPOSE_NAMES = ("docker-compose.yml", "compose.yml", "compose.yaml", "docker-compose.yaml")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
)
log = logging.getLogger("reconciler")


class _Handler(FileSystemEventHandler):
    def __init__(self, wake: threading.Event):
        self.wake = wake

    def on_any_event(self, event):
        self.wake.set()


def find_compose_file(project_dir: Path) -> Path | None:
    for name in COMPOSE_NAMES:
        p = project_dir / name
        if p.is_file():
            return p
    return None


def host_dir_for(slug: str) -> Path | None:
    if HOST_PROJECTS_DIR is None:
        return None
    return HOST_PROJECTS_DIR / slug


def list_slugs() -> list[str]:
    if not PROJECTS_DIR.is_dir():
        return []
    slugs = []
    for p in PROJECTS_DIR.iterdir():
        if not p.is_dir():
            continue
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        slugs.append(p.name)
    return sorted(slugs)


def recover_orphan_state(slug: str, project_dir: Path) -> None:
    status = read_status(project_dir)
    if not status:
        return
    state = status.get("state")
    if state not in ("pending", "building", "removing"):
        return
    last_action = status.get("last_action") or "unknown"
    write_status(
        project_dir,
        state="failed",
        containers=[],
        last_action=last_action,
        last_error=f"reconciler restarted while state={state}; "
                   f"re-trigger with `touch DEPLOY`",
    )
    log.warning(f"orphan state recovery: {slug} was {state} → failed")


def adopt(slug: str, project_dir: Path) -> None:
    if (project_dir / ".reconciler" / "status.json").exists():
        return
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        return
    rc, containers = compose_ps(project_dir, compose_file, slug, host_dir_for(slug))
    if rc != 0 or not containers:
        return

    hashes = compute_hashes(project_dir)
    write_applied_hashes(project_dir, hashes)
    state = "up" if all(c.get("state") == "running" for c in containers) else "degraded"
    write_status(
        project_dir,
        state=state,
        containers=containers,
        last_action="adopted",
        last_error=None,
    )
    log.info(f"adopted {slug}: state={state}, {len(containers)} containers")


def process_apply(slug: str, project_dir: Path, hint: str, policy) -> None:
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        write_status(project_dir, state="failed", containers=[],
                     last_action=f"apply:{hint}",
                     last_error="no compose file in project directory")
        return

    write_status(project_dir, state="pending", containers=[],
                 last_action=f"apply:{hint}", last_error=None)

    errors = validate_compose(compose_file, slug, policy)
    if errors:
        write_status(project_dir, state="failed", containers=[],
                     last_action=f"apply:{hint}",
                     last_error="linter rejected compose:\n  " + "\n  ".join(errors))
        log.warning(f"{slug}: linter rejected ({len(errors)} errors)")
        return

    write_status(project_dir, state="building", containers=[],
                 last_action=f"apply:{hint}", last_error=None)
    log.info(f"{slug}: apply:{hint} starting")
    started = time.monotonic()

    host_dir = host_dir_for(slug)
    if hint == "site":
        rc, out = compose_restart(project_dir, compose_file, slug, host_dir, service="nginx")
    elif hint in ("backend", "bot"):
        rc, out = compose_up(project_dir, compose_file, slug, host_dir, service=hint)
    else:
        rc, out = compose_up(project_dir, compose_file, slug, host_dir, service=None)

    elapsed = time.monotonic() - started
    write_log(project_dir, out)

    if rc != 0:
        _, containers = compose_ps(project_dir, compose_file, slug, host_dir)
        tail = "\n".join(out.splitlines()[-50:])
        write_status(project_dir, state="failed", containers=containers,
                     last_action=f"apply:{hint}",
                     last_error=f"compose exited {rc}\n---\n{tail}")
        log.warning(f"{slug}: apply:{hint} FAILED in {elapsed:.0f}s (rc={rc})")
        return

    _, containers = compose_ps(project_dir, compose_file, slug, host_dir)
    state = "up" if containers and all(c.get("state") == "running" for c in containers) else "degraded"
    write_applied_hashes(project_dir, compute_hashes(project_dir))
    write_status(project_dir, state=state, containers=containers,
                 last_action=f"apply:{hint}", last_error=None)
    log.info(f"{slug}: apply:{hint} → {state} in {elapsed:.0f}s")


def process_remove(slug: str, project_dir: Path) -> None:
    compose_file = find_compose_file(project_dir)
    host_dir = host_dir_for(slug)
    if compose_file:
        write_status(project_dir, state="removing", containers=[],
                     last_action="remove", last_error=None)
        log.info(f"{slug}: remove starting")
        rc, out = compose_down(project_dir, compose_file, slug, host_dir)
        write_log(project_dir, out)
        if rc != 0:
            write_status(project_dir, state="failed", containers=[],
                         last_action="remove",
                         last_error=f"compose down exited {rc}")
            log.warning(f"{slug}: remove FAILED (rc={rc})")
            return
    _archive(project_dir)
    log.info(f"{slug}: removed and archived")


def _archive(project_dir: Path) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    archive_root = PROJECTS_DIR / ".archive"
    archive_root.mkdir(exist_ok=True)
    dest = archive_root / f"{project_dir.name}-{ts}"
    shutil.move(str(project_dir), str(dest))


def tick(q: "queue.Queue") -> None:
    for slug in list_slugs():
        project_dir = PROJECTS_DIR / slug

        remove = project_dir / "REMOVE"
        if remove.exists():
            _unlink_quiet(remove)
            q.put(("remove", slug, "full"))
            continue

        deploy = project_dir / "DEPLOY"
        if deploy.exists():
            _unlink_quiet(deploy)
            q.put(("apply", slug, "full"))
            continue

        if not (project_dir / ".reconciler" / "status.json").is_file():
            continue
        status = read_status(project_dir)
        if status and status.get("state") in ("pending", "building", "removing"):
            continue
        hint = diff_action(compute_hashes(project_dir), read_applied_hashes(project_dir))
        if hint:
            log.info(f"{slug}: hash diff → apply:{hint}")
            q.put(("apply", slug, hint))


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def worker_loop(q: "queue.Queue", policy) -> None:
    while True:
        action, slug, hint = q.get()
        project_dir = PROJECTS_DIR / slug
        try:
            if action == "apply":
                process_apply(slug, project_dir, hint, policy)
            elif action == "remove":
                process_remove(slug, project_dir)
        except Exception as e:
            log.exception(f"error processing {action} for {slug}")
            try:
                write_status(project_dir, state="failed", containers=[],
                             last_action=f"{action}:{hint}",
                             last_error=f"internal error: {e}")
            except Exception:
                pass
        finally:
            q.task_done()


def main() -> None:
    log.info(
        f"starting: PROJECTS_DIR={PROJECTS_DIR}, "
        f"HOST_PROJECTS_DIR={HOST_PROJECTS_DIR}, "
        f"POLICY_FILE={POLICY_FILE}, interval={RECONCILE_INTERVAL}s"
    )
    if HOST_PROJECTS_DIR is None:
        log.warning(
            "HOST_PROJECTS_DIR not set — bind-mounts in per-project compose will "
            "resolve against reconciler container paths (not host), which docker "
            "daemon cannot access. Set HOST_PROJECTS_DIR in docker-compose.yml."
        )

    # Bind-mount trap: если POLICY_FILE не существует на хосте, docker тихо
    # создаёт на его месте пустую директорию и монтирует её в контейнер.
    # Явная проверка даёт говорящую ошибку вместо "Errno 21: Is a directory".
    if POLICY_FILE.is_dir():
        log.error(
            f"POLICY_FILE {POLICY_FILE} is a directory, not a file. "
            f"Это типичная ловушка docker-compose bind-mount: исходный файл "
            f"на хосте не существовал, и daemon создал пустую директорию. "
            f"Останови контейнер, удали пустую директорию на хосте "
            f"(rmdir config/policy.yaml), скопируй config/policy.example.yaml "
            f"в config/policy.yaml и перезапусти."
        )
        sys.exit(2)
    if not POLICY_FILE.is_file():
        log.error(
            f"POLICY_FILE {POLICY_FILE} not found. Ожидался смонтированный "
            f"файл политики. Проверь volume в docker-compose.yml и что на "
            f"хосте лежит config/policy.yaml (скопируй из policy.example.yaml)."
        )
        sys.exit(2)
    try:
        policy = load_policy(POLICY_FILE)
    except RuntimeError as e:
        log.error(f"cannot load policy: {e}")
        sys.exit(2)
    log.info(
        f"policy loaded: {len(policy.reserved_slugs)} reserved slugs, "
        f"allowed_external={sorted(policy.allowed_external_networks)}, "
        f"name_prefix={policy.name_prefix!r}"
    )

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    for slug in list_slugs():
        project_dir = PROJECTS_DIR / slug
        try:
            recover_orphan_state(slug, project_dir)
        except Exception:
            log.exception(f"orphan recovery failed for {slug}")
        try:
            adopt(slug, project_dir)
        except Exception:
            log.exception(f"adopt failed for {slug}")

    q: queue.Queue = queue.Queue()
    threading.Thread(target=worker_loop, args=(q, policy), daemon=True).start()

    wake = threading.Event()
    observer = Observer()
    observer.schedule(_Handler(wake), str(PROJECTS_DIR), recursive=True)
    observer.start()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        while True:
            wake.wait(timeout=RECONCILE_INTERVAL)
            wake.clear()
            time.sleep(DEBOUNCE_SECONDS)
            try:
                tick(q)
            except Exception:
                log.exception("tick failed")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
