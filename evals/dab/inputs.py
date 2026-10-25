"""Pinned DAB inputs: fetch, verify and readiness states.

Preparation and validation make no inference calls. Everything downloaded or generated lands under the ignored
``.local/evals/dab`` tree; the tracked suite manifest is the only source of pins.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import PROTOCOL
from .suite import Case, Suite, SuiteError
from .suitegen import DIRECTORIES

SOURCE_URL = "https://codeload.github.com/ucbepic/DataAgentBench/tar.gz/{revision}"
MIRROR_URL = "https://huggingface.co/datasets/ruiyingm/DataAgentBench-data/resolve/main/{path}"
READY, BLOCKED = "ready", "blocked"


class InputError(RuntimeError):
    """A pinned input could not be fetched or does not match its pin."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Paths:
    """The evaluator's private tree. Nothing here is ever mounted into a sandbox."""

    root: Path

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def source(self) -> Path:
        return self.root / "source.tar.gz"

    @property
    def prepared(self) -> Path:
        return self.root / "prepared.json"

    @property
    def budget(self) -> Path:
        return self.root / "budget.json"

    def runs(self) -> Path:
        return self.root / "runs"

    def ensure(self) -> "Paths":
        self.root.mkdir(parents=True, exist_ok=True)
        return self


def _download(url: str, dest: Path, *, timeout: float = 120.0) -> None:
    """Stream a URL to a temporary sibling, then rename, so a partial download is never verified."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=timeout) as response, temporary.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)
    temporary.replace(dest)


def ensure_source(paths: Paths, revision: str) -> dict:
    """Fetch and extract the pinned DAB checkout once; return its artifact hash for reproducibility."""
    paths.ensure()
    if not paths.repo.is_dir():
        if not paths.source.is_file():
            _download(SOURCE_URL.format(revision=revision), paths.source)
        with tarfile.open(paths.source, "r:gz") as archive:
            members = archive.getmembers()
            roots = {member.name.split("/", 1)[0] for member in members}
            if len(roots) != 1 or any(".." in member.name.split("/") for member in members):
                raise InputError("the pinned archive does not have a single safe root directory")
            archive.extractall(paths.root / "extract", members=members, filter="data")
        (paths.root / "extract" / roots.pop()).rename(paths.repo)
        (paths.root / "extract").rmdir()
    return {"sha256": sha256_file(paths.source), "bytes": paths.source.stat().st_size}


def _fetch_if_needed(paths: Paths, repo: Path, relative: str, pin: dict) -> str:
    """Ensure one mirror file exists with the pinned bytes; return ``cached`` or ``mirror``."""
    path = repo / relative
    if path.is_file() and path.stat().st_size == pin["bytes"] and sha256_file(path) == pin["sha256"]:
        return "cached"
    _download(MIRROR_URL.format(path=relative), path)
    if path.stat().st_size != pin["bytes"] or sha256_file(path) != pin["sha256"]:
        raise InputError(f"{relative} does not match its pinned SHA-256 and byte count")
    return "mirror"


def _verify(case: Case, repo: Path) -> dict[str, str]:
    """Verify one case's query files; raise when any pin does not match."""
    query_dir = case.query_dir(repo)
    missing = [name for name in case.query.files if not (query_dir / name).is_file()]
    if missing:
        raise InputError(f"{case.case_id}: missing {', '.join(missing)}")
    if not case.verifies(repo):
        raise InputError(f"{case.case_id}: query files do not match the tracked pins")
    return dict(case.query.files)


def prepare(paths: Paths, suite: Suite, cases: list[Case], *, services: tuple[str, ...] = (),
            now: str | None = None) -> dict:
    """Fetch and verify the selected cases; write ``prepared.json``; never call a model.

    A dataset is ``ready`` only when its checkout files match their pins, every sanctioned store matches its bytes,
    and any required database service is declared available. Service-backed datasets stay ``blocked`` until the
    caller declares the service, so a missing server never silently degrades into reading files by hand.
    """
    paths.ensure()
    source = ensure_source(paths, suite.data["revision"])
    fetched: dict[str, str] = {}
    recorded: dict[str, dict] = {}
    for case in cases:
        try:
            query_pins = _verify(case, paths.repo)
        except InputError as exc:
            recorded[case.case_id] = {"status": BLOCKED, "reason": str(exc)}
            continue
        for relative, pin in case.dataset.data_files.items():
            try:
                if pin["source"] == "mirror":
                    repository_path = f"{DIRECTORIES[case.dataset.name]}/{relative}"
                    fetched[repository_path] = _fetch_if_needed(paths, paths.repo, repository_path, pin)
                else:
                    path = case.dataset.directory(paths.repo) / relative
                    if not path.is_file() or sha256_file(path) != pin["sha256"]:
                        raise InputError(f"{case.dataset.name}: {relative} does not match its pinned bytes")
            except InputError as exc:
                recorded[case.case_id] = {"status": BLOCKED, "reason": str(exc)}
                break
        else:
            required = case.dataset.service_dbms
            missing = sorted(required - set(services))
            if missing:
                recorded[case.case_id] = {
                    "status": BLOCKED, "reason": f"requires database service: {', '.join(missing)}"}
            else:
                recorded[case.case_id] = {"status": READY, "reason": None, "query_files": query_pins,
                                          "data_files": {name: pin["sha256"]
                                                         for name, pin in case.dataset.data_files.items()}}
    prepared = {
        "protocol": PROTOCOL, "revision": suite.data["revision"],
        "prepared_at": now or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "keel": keel_state(Path.cwd()), "source": source, "services": list(services),
        "cases": recorded, "fetched": dict(sorted(fetched.items())),
    }
    paths.prepared.write_text(json.dumps(prepared, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return prepared


def readiness(paths: Paths, suite: Suite, cases: list[Case], *, services: tuple[str, ...] = ()) -> dict[str, dict]:
    """Re-verify the selected cases offline; no downloads and no inference.

    The result mirrors ``prepare``'s case map so ``validate`` can fail closed when an input changed after
    preparation.
    """
    if not paths.prepared.is_file():
        raise InputError("prepared.json is missing; run prepare first")
    prepared = json.loads(paths.prepared.read_text(encoding="utf-8"))
    if prepared.get("protocol") != PROTOCOL or prepared.get("revision") != suite.data["revision"]:
        raise InputError("prepared.json belongs to another protocol or revision")
    result: dict[str, dict] = {}
    for case in cases:
        prior = prepared.get("cases", {}).get(case.case_id)
        if prior is None:
            result[case.case_id] = {"status": BLOCKED, "reason": "not prepared"}
            continue
        if prior["status"] != READY:
            result[case.case_id] = dict(prior)
            continue
        try:
            _verify(case, paths.repo)
            for relative, pin in case.dataset.data_files.items():
                path = case.dataset.directory(paths.repo) / relative
                if not path.is_file() or path.stat().st_size != pin["bytes"] or sha256_file(path) != pin["sha256"]:
                    raise InputError(f"{case.dataset.name}: {relative} changed since preparation")
        except InputError as exc:
            result[case.case_id] = {"status": BLOCKED, "reason": str(exc)}
            continue
        required = case.dataset.service_dbms
        missing = sorted(required - set(services))
        result[case.case_id] = ({"status": BLOCKED, "reason": f"requires database service: {', '.join(missing)}"}
                                if missing else {"status": READY, "reason": None})
    return result


def keel_state(root: Path) -> dict:
    """The product revision and dirty state, so a score is tied to the runtime it measured.

    Missing git (a source archive) is reported as unknown, never guessed.
    """
    def git(*args: str) -> str | None:
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True,
                                  timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    revision = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {"revision": revision, "dirty": None if status is None else bool(status)}


def load_prepared(paths: Paths) -> dict:
    if not paths.prepared.is_file():
        raise InputError("prepared.json is missing; run prepare first")
    return json.loads(paths.prepared.read_text(encoding="utf-8"))


def require_ready(result: dict[str, dict], expected: list[Case]) -> None:
    """Refuse to execute when any selected case is not ready; the report keeps the blocked cases."""
    blocked = {case.case_id: result.get(case.case_id, {}).get("reason", "not prepared")
               for case in expected if result.get(case.case_id, {}).get("status") != READY}
    if blocked:
        detail = "; ".join(f"{case_id}: {reason}" for case_id, reason in sorted(blocked.items()))
        raise SuiteError(f"cases are not ready: {detail}")
