"""Sandbox isolation for one DAB case: the policy, the per-profile mount tree and store descriptions.

The policy is built from the tracked suite only. A case whose stores are all files gets ``network: "none"``; a
service-backed case must name the internal benchmark network, so a missing server can never fall back to the
network by accident. Mount sources live under ``<mount_root>/<sha256(profile_id)>`` as Keel requires and are
removed after the run.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from keel.sandbox.base import profile_mount_root

from .suite import Case

DEFAULT_IMAGE = "keel-dab-eval:1"
WORKDIR = "/work"
DATA = "/data"
_FILESYSTEM = {"sqlite": "SQLite", "duckdb": "DuckDB"}


class IsolationError(RuntimeError):
    """The case cannot be isolated with the declared services."""


@dataclass(frozen=True)
class Isolation:
    """One profile's staged mounts plus the policy derived from them."""

    mount_dir: Path
    policy: dict
    stores: list[tuple[str, str]]

    def cleanup(self) -> None:
        shutil.rmtree(self.mount_dir, ignore_errors=True)


def _link(source: Path, target: Path) -> None:
    """Materialize a read-only mount source cheaply: hard link, then copy across filesystems."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def isolation(case: Case, repo: Path, mount_root: Path, profile_id: str, *,
              image: str = DEFAULT_IMAGE, service_network: str | None = None,
              services: tuple[str, ...] = ()) -> Isolation:
    """Stage one case for one profile and return its policy and store descriptions.

    Each sanctioned data file is linked into the profile's own directory, so no profile sees another's tree and the
    evaluator directory is never mounted. Service-backed cases require ``service_network`` and the declared service.
    """
    required = case.dataset.service_dbms
    if required:
        missing = sorted(required - set(services))
        if missing:
            raise IsolationError(f"{case.case_id}: requires database service: {', '.join(missing)}")
        if not service_network:
            raise IsolationError(f"{case.case_id}: requires an internal service network")
        network: str | None = service_network
    else:
        if service_network is not None:
            raise IsolationError(f"{case.case_id}: a file-backed case must not join a network")
        network = "none"
    profile_dir = Path(profile_mount_root(str(mount_root), profile_id))
    for relative in case.dataset.data_files:
        path = case.dataset.directory(repo) / relative
        if not path.is_file():
            raise IsolationError(f"{case.case_id}: sanctioned file {relative} is missing")
        _link(path, profile_dir / relative)
    stores: list[tuple[str, str]] = []
    for name, client in case.dataset.db_clients.items():
        kind = client["db_type"]
        if kind in _FILESYSTEM:
            stores.append((name, f"{_FILESYSTEM[kind]} database file at {DATA}/{client['db_path']} (read-only)"))
        elif kind == "postgres":
            stores.append((name, f"PostgreSQL database `{client['db_name']}` (read-only, connection in PGHOST etc.)"))
        elif kind == "mongo":
            stores.append((name, f"MongoDB database `{client['db_name']}` (read-only, connection in MONGO_URI)"))
        else:
            raise IsolationError(f"{case.case_id}: unknown db_type {kind!r}")
    policy = {
        "image": image,
        "workdir": WORKDIR,
        "network": network,
        "mounts": [{"source": str(profile_dir), "target": DATA, "read_only": True}],
    }
    return Isolation(mount_dir=profile_dir, policy=policy, stores=stores)


def profile_id_for(case: Case, repetition: int) -> str:
    """A deterministic profile id per case and repetition, so a rerun collides visibly instead of reusing state."""
    digest = hashlib.sha256(f"{case.case_id}#{repetition}".encode("utf-8")).hexdigest()[:20]
    return f"dab-{digest}"
