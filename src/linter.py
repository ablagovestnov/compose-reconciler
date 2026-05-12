"""Compose file validator — enforces policy rules before apply."""
import re
from collections import defaultdict
from pathlib import Path

import yaml

from policy import Policy

# `traefik.http.routers.<router>.<key...>`
_ROUTER_LABEL_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.(.+)$")


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

        errors.extend(_validate_traefik_labels(svc_name, svc, policy))

    return errors


def _validate_traefik_labels(svc_name: str, svc: dict, policy: Policy) -> list[str]:
    """Verify Traefik labels are complete enough to actually route + serve TLS.

    Catches the common 'simplified compose' bug where tls=true is declared
    but tls.certresolver is missing — Traefik silently falls back to its
    default self-signed cert and the landing fails browser TLS validation.
    """
    errors: list[str] = []
    prefix = f"service '{svc_name}':"
    labels = _service_labels(svc)
    if not labels:
        return errors

    enable = labels.get("traefik.enable", "").lower() == "true"
    networks = _service_networks(svc)
    on_proxy = any(n in policy.allowed_external_networks for n in networks)

    # 1) Public service must opt into Traefik explicitly.
    if on_proxy and not enable and not _has_any_traefik_router(labels):
        errors.append(f"{prefix} on a public network but no 'traefik.enable=true' / no router labels")

    if not _has_any_traefik_router(labels):
        return errors

    # 2) Multi-network containers must pin which network Traefik dials.
    #    Without this, Traefik may pick the internal bridge IP and 504.
    if len([n for n in networks if n]) >= 2 and "traefik.docker.network" not in labels:
        errors.append(
            f"{prefix} has multiple networks but missing 'traefik.docker.network=<name>' — "
            f"Traefik will guess the wrong IP")

    # 3) Per-router checks: tls=true ⇒ certresolver + entrypoints.
    routers: dict[str, dict[str, str]] = defaultdict(dict)
    for key, val in labels.items():
        m = _ROUTER_LABEL_RE.match(key)
        if m:
            routers[m.group(1)][m.group(2)] = str(val)

    allowed_resolvers = policy.traefik_allowed_certresolvers
    for router, keys in routers.items():
        if "rule" not in keys:
            errors.append(f"{prefix} router '{router}' has no 'rule=' label")

        tls_on = keys.get("tls", "").lower() == "true"
        has_resolver = "tls.certresolver" in keys
        # `tls.certresolver=...` alone implies tls=true, even without explicit `tls=true`.
        if has_resolver or tls_on:
            if "entrypoints" not in keys:
                errors.append(
                    f"{prefix} router '{router}' uses TLS but has no 'entrypoints=' label "
                    f"(usually 'websecure')")
            if not has_resolver:
                expected = (f"one of {sorted(allowed_resolvers)}" if allowed_resolvers
                            else "any configured resolver")
                errors.append(
                    f"{prefix} router '{router}' has tls=true but no 'tls.certresolver=' — "
                    f"Traefik will serve its default self-signed cert "
                    f"(set certresolver to {expected})")
            elif allowed_resolvers and keys["tls.certresolver"] not in allowed_resolvers:
                errors.append(
                    f"{prefix} router '{router}' tls.certresolver='{keys['tls.certresolver']}' "
                    f"is not in allowed set {sorted(allowed_resolvers)}")

    return errors


def _service_labels(svc: dict) -> dict[str, str]:
    """Normalize compose label syntax (list 'k=v' OR dict {k: v}) → flat dict."""
    raw = svc.get("labels")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            s = str(item)
            if "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
            else:
                out[s.strip()] = ""
        return out
    return {}


def _has_any_traefik_router(labels: dict[str, str]) -> bool:
    return any(_ROUTER_LABEL_RE.match(k) for k in labels)


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
