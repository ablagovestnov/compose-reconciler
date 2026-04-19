"""Compose file validator — enforces policy rules before apply."""
from pathlib import Path

import yaml

from policy import Policy


def validate_compose(compose_file: Path, slug: str, policy: Policy) -> list[str]:
    """Return list of errors. Empty list = valid."""
    errors: list[str] = []

    if not policy.slug_pattern.match(slug):
        errors.append(f"slug '{slug}' does not match {policy.slug_pattern.pattern}")
    if slug in policy.reserved_slugs:
        errors.append(f"slug '{slug}' is reserved")

    try:
        raw = compose_file.read_text()
    except OSError as e:
        return [f"cannot read {compose_file}: {e}"]

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return errors + [f"cannot parse {compose_file.name}: {e}"]

    if not isinstance(data, dict):
        return errors + [f"{compose_file.name}: root must be a mapping"]

    expected_name = policy.compose_name(slug)
    name = data.get("name")
    if name is not None and name != expected_name:
        errors.append(f"compose 'name:' must be '{expected_name}' (got '{name}')")

    services = data.get("services")
    if not isinstance(services, dict) or not services:
        errors.append("compose has no services")
        return errors

    declared_networks = data.get("networks") or {}
    internal_net = policy.internal_network_name(slug)
    container_prefix = policy.container_name_prefix(slug)

    for svc_name, svc in services.items():
        prefix = f"service '{svc_name}':"
        if not isinstance(svc, dict):
            errors.append(f"{prefix} must be a mapping")
            continue

        cn = svc.get("container_name")
        if not cn:
            errors.append(f"{prefix} missing container_name")
        elif not str(cn).startswith(container_prefix):
            errors.append(f"{prefix} container_name must start with '{container_prefix}' (got '{cn}')")

        for k in policy.forbidden_service_keys:
            if k in svc:
                errors.append(f"{prefix} '{k}:' is not allowed")

        if "ports" in svc:
            errors.append(f"{prefix} 'ports:' not allowed — use reverse-proxy labels only")

        for vol in svc.get("volumes", []) or []:
            src = _volume_source(vol)
            if not src or not src.startswith("/"):
                continue
            for bad in policy.forbidden_mount_prefixes:
                if src == bad or src.startswith(bad + "/"):
                    errors.append(f"{prefix} mount of host path '{src}' is not allowed")
                    break

        for n in _service_networks(svc):
            if n == internal_net:
                continue
            if n in policy.allowed_external_networks:
                net_def = declared_networks.get(n)
                if isinstance(net_def, dict) and net_def.get("external"):
                    continue
                errors.append(f"{prefix} network '{n}' must be declared as external: true")
                continue
            errors.append(f"{prefix} network '{n}' not allowed (only '{internal_net}' or one of {sorted(policy.allowed_external_networks)})")

    return errors


def _volume_source(vol) -> str:
    if isinstance(vol, str):
        return vol.split(":", 1)[0]
    if isinstance(vol, dict):
        return str(vol.get("source", ""))
    return ""


def _service_networks(svc: dict) -> list[str]:
    nets = svc.get("networks")
    if isinstance(nets, list):
        return [str(n) for n in nets]
    if isinstance(nets, dict):
        return list(nets.keys())
    return []
