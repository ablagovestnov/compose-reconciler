"""Wrapper around `docker compose` subprocess with secret masking."""
import json
import os
import re
import subprocess
from pathlib import Path

COMPOSE_TIMEOUT = int(os.environ.get("COMPOSE_TIMEOUT", "900"))
NAME_PREFIX = os.environ.get("COMPOSE_NAME_PREFIX", "project-")

_SECRET_RE = re.compile(
    r"(?i)(\b[a-z_][a-z0-9_]*(?:token|key|secret|password|pwd)[a-z0-9_]*\s*[:=]\s*)(\S+)"
)


def mask_secrets(text: str) -> str:
    if not text:
        return text
    return _SECRET_RE.sub(lambda m: m.group(1) + "***", text)


def _run(cmd: list[str], cwd: Path, timeout: int = COMPOSE_TIMEOUT) -> tuple[int, str]:
    env = {**os.environ, "DOCKER_BUILDKIT": "1", "COMPOSE_DOCKER_CLI_BUILD": "1"}
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        combined = (r.stdout or "") + (r.stderr or "")
        return r.returncode, mask_secrets(combined)
    except subprocess.TimeoutExpired as e:
        tail = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        return 124, f"TIMEOUT after {timeout}s\n{mask_secrets(tail)}"
    except FileNotFoundError:
        return 127, f"docker CLI not found: {cmd[0]}"
    except Exception as e:
        return 1, f"runner error: {e}"


def _base(compose_file: Path, slug: str, host_project_dir: Path | None) -> list[str]:
    """Base compose command. host_project_dir is needed so the docker daemon
    can resolve relative bind-mounts (`./data`, `./site`) against host paths,
    not paths inside the reconciler container.
    """
    cmd = ["docker", "compose", "-p", f"{NAME_PREFIX}{slug}", "-f", str(compose_file)]
    if host_project_dir is not None:
        cmd += ["--project-directory", str(host_project_dir)]
    return cmd


def compose_up(project_dir: Path, compose_file: Path, slug: str,
               host_project_dir: Path | None,
               service: str | None = None) -> tuple[int, str]:
    cmd = _base(compose_file, slug, host_project_dir) + ["up", "-d", "--build"]
    if service:
        cmd.append(service)
    return _run(cmd, cwd=project_dir)


def compose_restart(project_dir: Path, compose_file: Path, slug: str,
                    host_project_dir: Path | None,
                    service: str) -> tuple[int, str]:
    cmd = _base(compose_file, slug, host_project_dir) + ["restart", service]
    return _run(cmd, cwd=project_dir, timeout=60)


def compose_down(project_dir: Path, compose_file: Path, slug: str,
                 host_project_dir: Path | None) -> tuple[int, str]:
    cmd = _base(compose_file, slug, host_project_dir) + ["down", "-v"]
    return _run(cmd, cwd=project_dir)


def compose_ps(project_dir: Path, compose_file: Path, slug: str,
               host_project_dir: Path | None) -> tuple[int, list[dict]]:
    cmd = _base(compose_file, slug, host_project_dir) + ["ps", "--format", "json", "-a"]
    rc, out = _run(cmd, cwd=project_dir, timeout=30)
    if rc != 0:
        return rc, []

    parsed: list[dict] = []
    stripped = out.strip()
    if not stripped:
        return 0, []

    try:
        data = json.loads(stripped)
        parsed = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    containers = []
    for item in parsed:
        containers.append({
            "name": item.get("Name") or item.get("Service") or "?",
            "state": str(item.get("State") or "").lower(),
            "health": item.get("Health") or None,
        })
    return 0, containers
