#!/usr/bin/env python3
"""Build the static Linux supervisor helper and its deterministic manifest.

The helper is never downloaded: it is compiled from `native/supervisor` inside a throwaway container running the
repository's Python base image with musl-tools, gcc and make. One container is used per target architecture, the
repository is mounted read-only and only the output directory is writable. The host then verifies the ELF and writes
`manifest.json`, which records the protocol version (read from `supervisor.h`) and each artifact's SHA-256.

    scripts/build_supervisor.py --arch all
    scripts/build_supervisor.py --arch amd64 --output /some/dir
    scripts/build_supervisor.py --binary /out/keel-supervisor --output src/keel/sandbox/bin

`--binary` packages an existing native build (for example one compiled by a Dockerfile build stage) and derives the
architecture from the ELF header, so a multi-arch image build never hardcodes one architecture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "src" / "keel" / "sandbox" / "bin"
BUILD_IMAGE = os.environ.get("KEEL_SUPERVISOR_BUILD_IMAGE", "python:3.12-slim")
MANIFEST_VERSION = 1
ARCHITECTURES = ("amd64", "arm64")
_ELF_MACHINES = {62: "amd64", 183: "arm64"}
_ELF_HEADER = 64
_PT_LOAD, _PT_DYNAMIC, _PT_INTERP = 1, 2, 3
_PROTOCOL = re.compile(r"^\s*#define\s+KEEL_PROTOCOL_VERSION\s+(\d+)\s*$", re.MULTILINE)
_MAX_BINARY_BYTES = 64 * 1024 * 1024

# Build tooling is installed at build time (never at run time); the supervisor itself is compiled with musl-gcc and
# linked statically so it runs in any Linux sandbox image without a dynamic loader.
_BUILD_COMMAND = (
    "set -eu; "
    "apt-get update; "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends musl-tools gcc make; "
    "rm -rf /var/lib/apt/lists/*; "
    "make -C /src/native/supervisor BUILD=/out CC=musl-gcc CFLAGS='-O2 -g -static' all; "
    f"chown {os.getuid()}:{os.getgid()} /out/keel-supervisor"
)


class BuildError(RuntimeError):
    """The helper could not be built or verified; the message says what to fix."""


def protocol_version() -> int:
    header = REPO_ROOT / "native" / "supervisor" / "supervisor.h"
    try:
        text = header.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read {header}: {exc}") from exc
    match = _PROTOCOL.search(text)
    if match is None:
        raise BuildError(f"{header} does not define KEEL_PROTOCOL_VERSION")
    return int(match.group(1))


def elf_details(path: Path) -> tuple[str, list[str]]:
    """Return the architecture and any static-linking problems found in the ELF header and program headers."""
    try:
        size = path.stat().st_size
        data = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"cannot read the supervisor binary {path}: {exc}") from exc
    if size > _MAX_BINARY_BYTES:
        raise BuildError(f"the supervisor binary {path} is unexpectedly large")
    if len(data) < _ELF_HEADER or data[:4] != b"\x7fELF":
        raise BuildError(f"{path} is not an ELF binary")
    if data[4] != 2 or data[5] != 1:
        raise BuildError(f"{path} must be a 64-bit little-endian ELF binary")
    kind = int.from_bytes(data[16:18], "little")
    machine = int.from_bytes(data[18:20], "little")
    architecture = _ELF_MACHINES.get(machine)
    if architecture is None:
        raise BuildError(f"{path} is built for an unsupported ELF machine {machine}")
    if kind not in (2, 3):  # ET_EXEC or a static ET_DYN; both run without a loader here
        raise BuildError(f"{path} is not an executable ELF binary")
    offset = int.from_bytes(data[32:40], "little")
    entry_size = int.from_bytes(data[54:56], "little")
    count = int.from_bytes(data[56:58], "little")
    if entry_size < 56 or count == 0:
        raise BuildError(f"{path} has no readable program headers")
    if offset > len(data) or count * entry_size > len(data) - offset:
        raise BuildError(f"{path} has a truncated program header table")
    problems = []
    for index in range(count):
        start = offset + index * entry_size
        if start + 56 > len(data):
            raise BuildError(f"{path} has a truncated program header")
        segment = int.from_bytes(data[start:start + 4], "little")
        if segment == _PT_INTERP:
            problems.append("it requests a dynamic loader (PT_INTERP)")
        elif segment == _PT_DYNAMIC:
            problems.append("it has a dynamic section (PT_DYNAMIC)")
    return architecture, problems


def verify_binary(path: Path, expected: str | None) -> str:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise BuildError(f"{path} is missing or not an executable regular file")
    architecture, problems = elf_details(path)
    if problems:
        raise BuildError(f"{path} is not a static binary: " + "; ".join(problems))
    if expected is not None and architecture != expected:
        raise BuildError(f"{path} is a {architecture} binary, not {expected}")
    return architecture


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _docker(*args: str, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except FileNotFoundError as exc:
        raise BuildError(
            "the docker CLI is required to build the supervisor; use --binary with an existing static build "
            "when no daemon is available") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"docker {args[0]} did not finish within {timeout:.0f}s") from exc


def _require_daemon() -> None:
    result = _docker("info", "--format", "{{.ServerVersion}}", timeout=60)
    if result.returncode != 0:
        raise BuildError("the docker daemon is not reachable: " + result.stderr.strip()[:300])


def _build_in_container(architecture: str, staging: Path) -> None:
    result = _docker(
        "run", "--rm", "--platform", f"linux/{architecture}",
        "-v", f"{REPO_ROOT}:/src:ro", "-v", f"{staging}:/out", BUILD_IMAGE,
        "sh", "-c", _BUILD_COMMAND, timeout=1800)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-800:]
        hint = ""
        lowered = detail.lower()
        if "exec format error" in lowered or "no matching manifest" in lowered:
            hint = (" Foreign-architecture emulation is unavailable in Docker: enable it for "
                    f"linux/{architecture} or build on that architecture.")
        raise BuildError(f"building the {architecture} supervisor failed: {detail}{hint}")


def build_architecture(architecture: str, output: Path) -> Path:
    if architecture not in ARCHITECTURES:
        raise BuildError(f"unsupported architecture {architecture!r}")
    staging = Path(tempfile.mkdtemp(prefix=".supervisor-", dir=output))
    try:
        _build_in_container(architecture, staging)
        path = staging / "keel-supervisor"
        verify_binary(path, architecture)
        destination = output / f"keel-supervisor-{architecture}"
        shutil.copyfile(path, destination)
        destination.chmod(0o755)
        return destination
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def package_binary(binary: Path, output: Path) -> tuple[str, Path]:
    architecture = verify_binary(binary, None)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"keel-supervisor-{architecture}"
    if destination.exists() and binary.resolve() == destination.resolve():
        destination.chmod(0o755)
    else:
        shutil.copyfile(binary, destination)
        destination.chmod(0o755)
    return architecture, destination


def write_manifest(output: Path, artifacts: dict[str, Path], protocol: int) -> Path:
    entries = {architecture: {"file": path.name, "sha256": _digest(path)}
               for architecture, path in artifacts.items()}
    payload = {"version": MANIFEST_VERSION, "protocol": protocol, "artifacts": entries}
    path = output / "manifest.json"
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arch", choices=(*ARCHITECTURES, "all"), help="target architecture (default: all)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"artifact directory (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--binary", type=Path,
                        help="package an existing static build instead of compiling in a container")
    arguments = parser.parse_args(argv)
    output = arguments.output.resolve()
    try:
        protocol = protocol_version()
        output.mkdir(parents=True, exist_ok=True)
        artifacts: dict[str, Path] = {}
        if arguments.binary is not None:
            if arguments.arch == "all":
                raise BuildError("--binary packages one architecture; omit --arch or name it explicitly")
            architecture, path = package_binary(arguments.binary, output)
            if arguments.arch is not None and arguments.arch != architecture:
                raise BuildError(f"--arch {arguments.arch} does not match the {architecture} binary")
            artifacts[architecture] = path
        else:
            targets = ARCHITECTURES if arguments.arch in (None, "all") else (arguments.arch,)
            _require_daemon()
            for architecture in targets:
                artifacts[architecture] = build_architecture(architecture, output)
        manifest = write_manifest(output, artifacts, protocol)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for architecture, path in sorted(artifacts.items()):
        print(f"{architecture}: {path} ({_digest(path)})")
    print(f"manifest: {manifest} (protocol {protocol})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
