"""Service settings, read from the environment."""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import json
import math
import os
import posixpath
import re
import socket
from typing import Mapping

_DB_SCHEMES = ("postgresql://", "postgres://", "sqlite:///")
_DEPLOYMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TENANT_INT_MIN = -2_147_483_648
_TENANT_INT_MAX = 2_147_483_647
# A canonical decimal: no sign on zero, no plus sign, no leading zeros.
_TENANT = re.compile(r"^(?:0|-?[1-9][0-9]{0,9})$")
_SECRET = re.compile(r"^[^\s,]+$")


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{name} must be a positive integer, not {raw!r}")
    return int(raw)


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number, not {raw!r}")
    return value


def _fraction(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not 0 < value <= 1:
        raise ValueError(f"{name} must be a number above 0 and at most 1, not {raw!r}")
    return value


def _instance_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


@dataclass(frozen=True)
class ServiceCredential:
    tenant_id: int
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.tenant_id) is not int or not _TENANT_INT_MIN <= self.tenant_id <= _TENANT_INT_MAX:
            raise ValueError("tenant id must be a 32-bit integer")


def tenant_id_for(credentials: tuple[ServiceCredential, ...], authorization: str) -> int | None:
    """The tenant bound to this exact bearer secret, or None. Every secret is compared."""
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return None
    supplied = authorization[len(prefix):].encode()
    found = None
    for credential in credentials:
        if hmac.compare_digest(supplied, credential.secret.encode()):
            found = credential.tenant_id
    return found


def _credential(tenant_id: str, secret: str, source: str) -> ServiceCredential:
    if not _TENANT.fullmatch(tenant_id) or not _TENANT_INT_MIN <= int(tenant_id) <= _TENANT_INT_MAX:
        raise ValueError(f"{source} tenant id must be a 32-bit integer")
    value = int(tenant_id)
    if not secret or not _SECRET.fullmatch(secret):
        raise ValueError(f"{source} entry must be tenant:secret")
    return ServiceCredential(value, secret)


def _credentials(env: Mapping[str, str]) -> tuple[ServiceCredential, ...]:
    found: list[ServiceCredential] = []
    raw_keys = (env.get("KEEL_API_KEYS") or "").strip()
    if raw_keys:
        for entry in raw_keys.split(","):
            entry = entry.strip()
            tenant_id, sep, secret = entry.partition(":")
            if not sep:
                raise ValueError(f"KEEL_API_KEYS entry {entry!r} must be tenant:secret")
            found.append(_credential(tenant_id, secret, "KEEL_API_KEYS"))
    api_key = (env.get("KEEL_API_KEY") or "").strip()
    tenant = (env.get("KEEL_API_KEY_TENANT") or "").strip()
    if api_key and not tenant:
        raise ValueError("KEEL_API_KEY_TENANT is required")
    if tenant and not api_key:
        raise ValueError("KEEL_API_KEY is required")
    if api_key:
        found.append(_credential(tenant, api_key, "KEEL_API_KEY"))
    if not found:
        raise ValueError("KEEL_API_KEY or KEEL_API_KEYS is required")
    secrets = [item.secret for item in found]
    if len(set(secrets)) != len(secrets):
        raise ValueError("A service key is bound to more than one tenant")
    return tuple(found)


def _backfill(env: Mapping[str, str]) -> dict[str, int]:
    path = (env.get("KEEL_TENANT_BACKFILL") or "").strip()
    if not path:
        return {}
    try:
        text = open(path, encoding="utf-8").read()
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise ValueError(f"KEEL_TENANT_BACKFILL cannot be read: {path}") from exc
    if not isinstance(data, dict) or not all(isinstance(k, str) and type(v) is int for k, v in data.items()):
        raise ValueError("KEEL_TENANT_BACKFILL must map profile ids to integer tenant ids")
    for profile_id, tenant_id in data.items():
        if not _ID.fullmatch(profile_id) or not _TENANT_INT_MIN <= tenant_id <= _TENANT_INT_MAX:
            raise ValueError("KEEL_TENANT_BACKFILL has an invalid profile or integer tenant id")
    return dict(data)


@dataclass(frozen=True)
class Settings:
    credentials: tuple[ServiceCredential, ...] = field(repr=False)
    database_url: str
    workers: int = 4
    max_running: int = 10
    lease_seconds: float = 30.0
    aws_region: str = "us-east-1"
    max_iterations: int = 60
    compression_threshold: float = 0.75
    event_retention_days: int = 7
    background_limit: int = 4
    memory_review_turns: int = 10
    memory_chars: int = 2200
    user_chars: int = 1375
    sandbox: str = "docker"
    sandbox_idle_seconds: float = 1800.0
    sandbox_mount_root: str | None = None
    docker: str = "docker"
    deployment: str = "keel"
    openai_api_key: str | None = field(default=None, repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_compat: bool = False
    openai_context_window: int = 128_000
    model_stall_seconds: float = 180.0
    instance_id: str = field(default_factory=_instance_id)
    tenant_backfill: dict[str, int] = field(default_factory=dict)

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql://", "postgres://"))

    @property
    def db_command_timeout(self) -> float:
        """A third of the lease, at least 5 s: one stalled query must not outlast a lease renewal and get a live
        run swept."""
        return max(5.0, self.lease_seconds / 3)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        credentials = _credentials(env)
        backfill = _backfill(env)
        url = (env.get("KEEL_DATABASE_URL") or "").strip()
        if not url.startswith(_DB_SCHEMES):
            raise ValueError("KEEL_DATABASE_URL must be postgresql://… or sqlite:///path")
        sandbox = (env.get("KEEL_SANDBOX") or "docker").strip()
        if sandbox not in ("docker", "none"):
            raise ValueError("KEEL_SANDBOX must be docker or none")
        mount_root = (env.get("KEEL_SANDBOX_MOUNT_ROOT") or "").strip() or None
        if mount_root is not None and not mount_root.startswith("/"):
            raise ValueError("KEEL_SANDBOX_MOUNT_ROOT must be an absolute path")
        if mount_root is not None:
            mount_root = posixpath.normpath(mount_root)
        deployment = (env.get("KEEL_DEPLOYMENT") or "keel").strip()
        if not _DEPLOYMENT.fullmatch(deployment):
            raise ValueError("KEEL_DEPLOYMENT must be 1-40 lowercase letters, digits or '-'")
        base_url = (env.get("KEEL_OPENAI_BASE_URL") or "").strip() or "https://api.openai.com/v1"
        if not base_url.lower().startswith(("http://", "https://")):
            raise ValueError("KEEL_OPENAI_BASE_URL must start with http:// or https://")
        return cls(
            credentials=credentials,
            database_url=url,
            workers=_positive_int(env, "KEEL_WORKERS", 4),
            max_running=_positive_int(env, "KEEL_MAX_RUNNING", 10),
            lease_seconds=_positive_float(env, "KEEL_LEASE_SECONDS", 30.0),
            aws_region=(env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1").strip(),
            max_iterations=_positive_int(env, "KEEL_MAX_ITERATIONS", 60),
            compression_threshold=_fraction(env, "KEEL_COMPRESSION_THRESHOLD", 0.75),
            event_retention_days=_positive_int(env, "KEEL_EVENT_RETENTION_DAYS", 7),
            background_limit=_positive_int(env, "KEEL_BACKGROUND_LIMIT", 4),
            memory_review_turns=_positive_int(env, "KEEL_MEMORY_REVIEW_TURNS", 10),
            memory_chars=_positive_int(env, "KEEL_MEMORY_CHARS", 2200),
            user_chars=_positive_int(env, "KEEL_USER_CHARS", 1375),
            sandbox=sandbox,
            sandbox_idle_seconds=_positive_float(env, "KEEL_SANDBOX_IDLE_SECONDS", 1800.0),
            sandbox_mount_root=mount_root,
            docker=(env.get("KEEL_DOCKER") or "").strip() or "docker",
            deployment=deployment,
            openai_api_key=(env.get("KEEL_OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or "").strip() or None,
            openai_base_url=base_url,
            openai_compat=(env.get("KEEL_OPENAI_COMPAT") or "").strip().lower() in ("1", "true", "yes"),
            openai_context_window=_positive_int(env, "KEEL_OPENAI_CONTEXT_WINDOW", 128_000),
            model_stall_seconds=_positive_float(env, "KEEL_MODEL_STALL_SECONDS", 180.0),
            tenant_backfill=backfill,
        )
