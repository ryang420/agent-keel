"""Service settings, read from the environment."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import posixpath
import re
import socket
from typing import Mapping

_DB_SCHEMES = ("postgresql://", "postgres://", "sqlite:///")
_DEPLOYMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


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


def _instance_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    database_url: str
    workers: int = 4
    max_running: int = 10
    lease_seconds: float = 30.0
    aws_region: str = "us-east-1"
    max_iterations: int = 60
    sandbox: str = "docker"
    sandbox_idle_seconds: float = 1800.0
    sandbox_mount_root: str | None = None
    docker: str = "docker"
    deployment: str = "keel"
    openai_api_key: str | None = field(default=None, repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_compat: bool = False
    openai_context_window: int = 128_000
    instance_id: str = field(default_factory=_instance_id)

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql://", "postgres://"))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        api_key = (env.get("KEEL_API_KEY") or "").strip()
        if not api_key:
            raise ValueError("KEEL_API_KEY is required")
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
            api_key=api_key,
            database_url=url,
            workers=_positive_int(env, "KEEL_WORKERS", 4),
            max_running=_positive_int(env, "KEEL_MAX_RUNNING", 10),
            lease_seconds=_positive_float(env, "KEEL_LEASE_SECONDS", 30.0),
            aws_region=(env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1").strip(),
            max_iterations=_positive_int(env, "KEEL_MAX_ITERATIONS", 60),
            sandbox=sandbox,
            sandbox_idle_seconds=_positive_float(env, "KEEL_SANDBOX_IDLE_SECONDS", 1800.0),
            sandbox_mount_root=mount_root,
            docker=(env.get("KEEL_DOCKER") or "").strip() or "docker",
            deployment=deployment,
            openai_api_key=(env.get("KEEL_OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or "").strip() or None,
            openai_base_url=base_url,
            openai_compat=(env.get("KEEL_OPENAI_COMPAT") or "").strip().lower() in ("1", "true", "yes"),
            openai_context_window=_positive_int(env, "KEEL_OPENAI_CONTEXT_WINDOW", 128_000),
        )
