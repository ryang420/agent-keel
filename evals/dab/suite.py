"""The tracked DAB suite manifest: frozen membership, pins and case helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import PROTOCOL
from .suitegen import DIRECTORIES

SMOKE_DATASETS = ("stockindex", "music_brainz_20k")
FILE_DBMS = frozenset({"sqlite", "duckdb"})
SERVICE_DBMS = frozenset({"postgres", "mongo"})
EXPECTED_QUERIES = {
    "agnews": 4, "bookreview": 3, "crmarenapro": 13, "DEPS_DEV_V1": 2, "GITHUB_REPOS": 4,
    "googlelocal": 4, "music_brainz_20k": 3, "pancancer_atlas": 3, "patents": 3, "stockindex": 3,
    "stockmarket": 5, "yelp": 7,
}
_HASH = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


class SuiteError(ValueError):
    """The tracked manifest is malformed or does not match the frozen membership."""


@dataclass(frozen=True)
class Query:
    """One pinned query: the hashes of the files an execution reads and is scored by."""

    number: str
    files: dict[str, str]

    @property
    def slug(self) -> str:
        return f"query{self.number}"

    def verifies(self, query_dir: Path) -> bool:
        """True when every file the manifest pins still has the pinned bytes."""
        from .suitegen import sha256_file

        return all((query_dir / name).is_file() and sha256_file(query_dir / name) == digest
                   for name, digest in self.files.items())


@dataclass(frozen=True)
class Dataset:
    """One dataset's sanctioned stores and its pinned queries."""

    name: str
    dbms: tuple[str, ...]
    db_clients: dict[str, dict[str, str]]
    data_files: dict[str, dict]
    queries: dict[str, Query]

    @property
    def service_dbms(self) -> frozenset[str]:
        return frozenset(self.dbms) & SERVICE_DBMS

    def directory(self, repo: Path) -> Path:
        return repo / DIRECTORIES[self.name]

    def store_paths(self) -> dict[str, str]:
        """Logical store name -> path relative to the dataset directory (files only)."""
        return {name: client.get("db_path") or client.get("sql_file") or client["dump_folder"]
                for name, client in self.db_clients.items()}


class Suite:
    """A validated view over ``suite.json``."""

    def __init__(self, data: dict, *, path: Path | None = None) -> None:
        self.path = path
        self.data = data
        self._validate()
        self.datasets: dict[str, Dataset] = {}
        for name, raw in data["datasets"].items():
            self.datasets[name] = Dataset(
                name=name, dbms=tuple(raw["dbms"]), db_clients=raw["db_clients"],
                data_files=raw["data_files"],
                queries={number: Query(number=number, files=files) for number, files in raw["queries"].items()})

    def _validate(self) -> None:
        if self.data.get("protocol") != PROTOCOL:
            raise SuiteError(f"protocol must be {PROTOCOL}")
        if not _REVISION.fullmatch(str(self.data.get("revision", ""))):
            raise SuiteError("revision must be a 40-digit commit")
        datasets = self.data.get("datasets")
        if not isinstance(datasets, dict) or set(datasets) != set(DIRECTORIES):
            raise SuiteError(f"datasets must be exactly the frozen membership: {sorted(DIRECTORIES)}")
        for name, raw in datasets.items():
            queries = raw.get("queries") or {}
            if len(queries) != EXPECTED_QUERIES[name]:
                raise SuiteError(f"{name}: expected {EXPECTED_QUERIES[name]} queries, found {len(queries)}")
            for number, files in queries.items():
                if not number.isdigit() or str(int(number)) != number:
                    raise SuiteError(f"{name}: query key {number!r} is not a canonical number")
                for filename, digest in files.items():
                    if filename not in ("query.json", "validate.py", "ground_truth.csv"):
                        raise SuiteError(f"{name}/{number}: unexpected pinned file {filename!r}")
                    if not _HASH.fullmatch(str(digest)):
                        raise SuiteError(f"{name}/{number}: {filename} is not pinned by a SHA-256")
                if set(files) != {"query.json", "validate.py", "ground_truth.csv"}:
                    raise SuiteError(f"{name}/{number}: query.json, validate.py and ground_truth.csv are all pinned")
            pinned = raw.get("data_files") or {}
            for client_name, client in raw["db_clients"].items():
                store = client.get("db_path") or client.get("sql_file") or client.get("dump_folder")
                if store is None:
                    raise SuiteError(f"{name}/{client_name}: store path is missing")
                if client["db_type"] == "mongo":
                    if not any(relative.startswith(store.rstrip("/") + "/") for relative in pinned):
                        raise SuiteError(f"{name}/{client_name}: mongo dump {store} has no pinned files")
                elif store not in pinned:
                    raise SuiteError(f"{name}/{client_name}: {store} has no pinned bytes")
                if client["db_type"] not in FILE_DBMS | SERVICE_DBMS:
                    raise SuiteError(f"{name}/{client_name}: unknown db_type {client['db_type']!r}")

    @classmethod
    def load(cls, path: Path) -> "Suite":
        return cls(json.loads(path.read_text(encoding="utf-8")), path=path)

    def cases(self, datasets: tuple[str, ...] | None = None) -> list[Case]:
        """Every case in the frozen order, optionally limited to the given datasets."""
        chosen = datasets or tuple(DIRECTORIES)
        unknown = set(chosen) - set(self.datasets)
        if unknown:
            raise SuiteError(f"unknown datasets: {sorted(unknown)}")
        return [Case(dataset=self.datasets[name], query=query)
                for name in chosen for query in self.datasets[name].queries.values()]

    def smoke(self) -> list[Case]:
        """Stage 0: the two file-backed smoke datasets, all six queries."""
        return self.cases(SMOKE_DATASETS)


@dataclass(frozen=True)
class Case:
    """A dataset and one pinned query."""

    dataset: Dataset
    query: Query

    @property
    def case_id(self) -> str:
        return f"{self.dataset.name}/{self.query.slug}"

    @property
    def query_dir_name(self) -> str:
        return self.query.slug

    def query_dir(self, repo: Path) -> Path:
        return self.dataset.directory(repo) / self.query.slug

    def verifies(self, repo: Path) -> bool:
        return self.query.verifies(self.query_dir(repo))
