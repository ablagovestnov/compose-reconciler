"""Policy loader — reads deployment-specific rules from a YAML file.

Policy schema (see config/policy.example.yaml for a full example):

    slug:
      pattern: '^[a-z][a-z0-9-]{1,30}$'  # optional, has default
      reserved: [api, admin, www, ...]

    compose:
      name_prefix: 'project-'            # compose `name:` must equal prefix+slug
      # container_name must start with prefix+slug+'-'
      # internal network:    prefix+slug+'-net'

    networks:
      allowed_external: [proxy]

    mounts:
      forbidden_prefixes: [/var/run, /etc, /root, ...]

    services:
      forbidden_keys: [privileged, pid, network_mode, cap_add, devices]
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_SLUG_PATTERN = r"^[a-z][a-z0-9-]{1,30}$"
_DEFAULT_NAME_PREFIX = "project-"


@dataclass
class Policy:
    slug_pattern: re.Pattern
    reserved_slugs: set[str]
    name_prefix: str
    allowed_external_networks: set[str]
    forbidden_mount_prefixes: list[str]
    forbidden_service_keys: set[str]

    def compose_name(self, slug: str) -> str:
        return f"{self.name_prefix}{slug}"

    def container_name_prefix(self, slug: str) -> str:
        return f"{self.name_prefix}{slug}-"

    def internal_network_name(self, slug: str) -> str:
        return f"{self.name_prefix}{slug}-net"


def load_policy(path: Path) -> Policy:
    try:
        raw = path.read_text()
    except OSError as e:
        raise RuntimeError(f"cannot read policy file {path}: {e}") from e

    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise RuntimeError(f"cannot parse policy file {path}: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError(f"policy file {path}: root must be a mapping")

    slug_cfg = data.get("slug") or {}
    compose_cfg = data.get("compose") or {}
    networks_cfg = data.get("networks") or {}
    mounts_cfg = data.get("mounts") or {}
    services_cfg = data.get("services") or {}

    pattern_str = slug_cfg.get("pattern", _DEFAULT_SLUG_PATTERN)
    try:
        pattern = re.compile(pattern_str)
    except re.error as e:
        raise RuntimeError(f"policy slug.pattern is not valid regex: {e}") from e

    return Policy(
        slug_pattern=pattern,
        reserved_slugs=set(slug_cfg.get("reserved") or []),
        name_prefix=str(compose_cfg.get("name_prefix", _DEFAULT_NAME_PREFIX)),
        allowed_external_networks=set(networks_cfg.get("allowed_external") or []),
        forbidden_mount_prefixes=list(mounts_cfg.get("forbidden_prefixes") or []),
        forbidden_service_keys=set(services_cfg.get("forbidden_keys") or []),
    )
