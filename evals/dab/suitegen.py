"""Generate the tracked DAB suite manifest from a pinned checkout.

Run at development time only, from the repository root:

    .venv/bin/python -m evals.dab.suitegen --repo .local/evals/dab/repo

The output ``evals/dab/suite.json`` pins the frozen membership: 12 documented datasets, 54 queries, and one SHA-256
per query file and sanctioned data file. ``prepare`` verifies a downloaded checkout against these pins; it never
regenerates them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from . import PROTOCOL

REVISION = "61fb21e0766d775acae443f6074f5d498e95f938"
MIRROR = "ruiyingm/DataAgentBench-data"
LFS_PREFIX = b"version https://git-lfs"

# Documented leaderboard membership: dataset id -> directory name in the pinned repository.
DIRECTORIES = {
    "agnews": "query_agnews",
    "bookreview": "query_bookreview",
    "crmarenapro": "query_crmarenapro",
    "DEPS_DEV_V1": "query_DEPS_DEV_V1",
    "GITHUB_REPOS": "query_GITHUB_REPOS",
    "googlelocal": "query_googlelocal",
    "music_brainz_20k": "query_music_brainz_20k",
    "pancancer_atlas": "query_PANCANCER_ATLAS",
    "patents": "query_PATENTS",
    "stockindex": "query_stockindex",
    "stockmarket": "query_stockmarket",
    "yelp": "query_yelp",
}
QUERY_KEYS = ("query.json", "validate.py", "ground_truth.csv")
_DB_FIELD = re.compile(r"^db_type:\s*(\S+)\s*$")
_CLIENT = re.compile(r"^  (\w+):\s*$")
_FIELD = re.compile(r"^    (db_type|db_name|db_path|sql_file|dump_folder):\s*(.+?)\s*$")


def sha256_file(path: Path) -> str:
    """Stream a file through SHA-256 so multi-gigabyte inputs do not load into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_lfs_pointer(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(LFS_PREFIX)) == LFS_PREFIX


def parse_db_config(path: Path) -> dict[str, dict[str, str]]:
    """Parse DAB's two-level ``db_config.yaml`` without a YAML dependency.

    Only the keys DAB uses are kept. Comments are stripped; every value is a plain string. A malformed file raises
    ``ValueError`` rather than silently dropping a database.
    """
    clients: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line.strip() == "db_clients:":
            continue
        if match := _CLIENT.match(line):
            current = clients.setdefault(match.group(1), {})
            continue
        if match := _FIELD.match(line):
            if current is None:
                raise ValueError(f"{path}:{number}: field outside a database client")
            current[match.group(1)] = match.group(2)
            continue
        if _DB_FIELD.match(line) or current is None:
            raise ValueError(f"{path}:{number}: unrecognised db_config line {raw!r}")
    for name, client in clients.items():
        if "db_type" not in client:
            raise ValueError(f"{path}: client {name!r} has no db_type")
        if client["db_type"] in ("sqlite", "duckdb") and "db_path" not in client:
            raise ValueError(f"{path}: client {name!r} has no db_path")
        if client["db_type"] == "postgres" and not {"db_name", "sql_file"} <= client.keys():
            raise ValueError(f"{path}: postgres client {name!r} needs db_name and sql_file")
        if client["db_type"] == "mongo" and not {"db_name", "dump_folder"} <= client.keys():
            raise ValueError(f"{path}: mongo client {name!r} needs db_name and dump_folder")
    return clients


def manifest_pins(repo: Path) -> dict[str, dict[str, int | str]]:
    """Read DAB's ``dataset_manifest.tsv`` into repo-relative path -> pin.

    The mirror holds the bytes for these paths. A row that does not parse is an error: the pin would be missing.
    """
    pins: dict[str, dict[str, int | str]] = {}
    for number, line in enumerate((repo / "dataset_manifest.tsv").read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            relative, digest, size = line.split("\t")
            pins[relative] = {"sha256": digest.strip(), "bytes": int(size)}
        except ValueError as exc:
            raise ValueError(f"dataset_manifest.tsv:{number}: expected three tab-separated fields") from exc
        if not re.fullmatch(r"[0-9a-f]{64}", pins[relative]["sha256"]):
            raise ValueError(f"dataset_manifest.tsv:{number}: sha256 is not 64 lowercase hex digits")
    return pins


def _data_files(dataset_dir: Path, clients: dict[str, dict[str, str]], pins: dict[str, dict[str, int | str]],
                repo: Path) -> dict[str, dict[str, int | str]]:
    """Pin every sanctioned store: mirror rows by their manifest hash, other real files by their own hash."""
    relative_root = dataset_dir.relative_to(repo).as_posix()
    files: dict[str, dict[str, int | str]] = {}
    for client in clients.values():
        if "db_path" in client:
            paths = [client["db_path"]]
        elif "sql_file" in client:
            paths = [client["sql_file"]]
        else:
            dump = dataset_dir / client["dump_folder"]
            if not dump.is_dir():
                raise ValueError(f"{dataset_dir.name}: mongo dump folder {client['dump_folder']} is missing")
            paths = [path.relative_to(dataset_dir).as_posix() for path in sorted(dump.rglob("*")) if path.is_file()]
        for relative in paths:
            repo_relative = f"{relative_root}/{relative}"
            if relative in files:
                continue
            if repo_relative in pins:
                files[relative] = {**pins[repo_relative], "source": "mirror"}
                continue
            path = dataset_dir / relative
            if not path.is_file():
                raise ValueError(f"{dataset_dir.name}: sanctioned store {relative} is missing")
            if is_lfs_pointer(path):
                raise ValueError(f"{dataset_dir.name}: {relative} is a Git LFS pointer with no mirror pin")
            files[relative] = {"sha256": sha256_file(path), "bytes": path.stat().st_size, "source": "repo"}
    return files


def build_suite(repo: Path) -> dict:
    """Assemble the frozen suite from a checkout of the pinned revision."""
    pins = manifest_pins(repo)
    datasets: dict[str, dict] = {}
    for dataset, directory in DIRECTORIES.items():
        dataset_dir = repo / directory
        if not dataset_dir.is_dir():
            raise ValueError(f"dataset {dataset}: directory {directory} is missing")
        clients = parse_db_config(dataset_dir / "db_config.yaml")
        queries: dict[str, dict[str, str]] = {}
        query_dirs = sorted((path for path in dataset_dir.glob("query[0-9]*") if path.is_dir()),
                            key=lambda path: int(path.name.removeprefix("query")))
        if not query_dirs:
            raise ValueError(f"dataset {dataset}: no query directories")
        for query_dir in query_dirs:
            files: dict[str, str] = {}
            for name in QUERY_KEYS:
                path = query_dir / name
                if not path.is_file():
                    raise ValueError(f"{dataset}/{query_dir.name}: {name} is missing")
                if is_lfs_pointer(path):
                    raise ValueError(f"{dataset}/{query_dir.name}: {name} is a Git LFS pointer")
                files[name] = sha256_file(path)
            queries[query_dir.name.removeprefix("query")] = files
        datasets[dataset] = {
            "dbms": sorted({client["db_type"] for client in clients.values()}),
            "db_clients": clients,
            "data_files": _data_files(dataset_dir, clients, pins, repo),
            "queries": queries,
        }
    return {"protocol": PROTOCOL, "revision": REVISION, "mirror": MIRROR, "datasets": datasets}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check the tracked DAB suite manifest.")
    parser.add_argument("--repo", default=".local/evals/dab/repo", help="pinned DataAgentBench checkout")
    parser.add_argument("--out", default="evals/dab/suite.json", help="tracked output path")
    parser.add_argument("--check", action="store_true", help="fail if the tracked manifest differs")
    args = parser.parse_args(argv)
    suite = build_suite(Path(args.repo))
    rendered = json.dumps(suite, indent=2, sort_keys=True) + "\n"
    output = Path(args.out)
    if args.check:
        if output.read_text(encoding="utf-8") != rendered:
            raise SystemExit(f"{output} is stale; rerun without --check")
        print(f"{output} matches {suite['revision']}: "
              f"{sum(len(d['queries']) for d in suite['datasets'].values())} queries")
        return 0
    output.write_text(rendered, encoding="utf-8")
    print(f"wrote {output}: {len(suite['datasets'])} datasets, "
          f"{sum(len(d['queries']) for d in suite['datasets'].values())} queries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
