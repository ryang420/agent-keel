#!/usr/bin/env python3
"""Verify that a built wheel, sdist or installed keel carries the sandbox helper it needs.

    scripts/verify_distribution.py wheel dist/agent_keel-*.whl --require amd64,arm64
    scripts/verify_distribution.py sdist dist/agent_keel-*.tar.gz
    docker run --rm -i IMAGE python - installed --require arm64 < scripts/verify_distribution.py

A wheel or installed package must contain `keel/sandbox/bin/manifest.json` (manifest version 1) naming an artifact for
every required architecture, each a static ELF of that architecture whose SHA-256 matches the manifest. An sdist must
contain the helper sources, its Makefile and Dockerfiles, the build script and the native test sources. Uses only
the standard library, so it also runs inside the runtime image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import zipfile

_MACHINES = {62: "amd64", 183: "arm64"}
_BIN = "keel/sandbox/bin/"
_SDIST_MEMBERS = (
    "native/supervisor/main.c", "native/supervisor/process.c", "native/supervisor/control.c",
    "native/supervisor/receipt.c", "native/supervisor/supervisor.h", "native/supervisor/Makefile",
    "native/supervisor/Dockerfile", "scripts/build_supervisor.py", "tests/native/supervisor_harness.c",
    "tests/native/pid_reuse.c", "Dockerfile",
)


class VerifyError(RuntimeError):
    pass


def _elf_architecture(data: bytes, name: str) -> str:
    if data[:4] != b"\x7fELF" or len(data) < 64 or data[4] != 2 or data[5] != 1:
        raise VerifyError(f"{name} is not a 64-bit little-endian ELF binary")
    architecture = _MACHINES.get(int.from_bytes(data[18:20], "little"))
    if architecture is None:
        raise VerifyError(f"{name} has an unsupported ELF machine")
    offset = int.from_bytes(data[32:40], "little")
    size = int.from_bytes(data[54:56], "little")
    count = int.from_bytes(data[56:58], "little")
    for index in range(count):
        start = offset + index * size
        if start + 4 > len(data):
            raise VerifyError(f"{name} has a truncated program header table")
        if int.from_bytes(data[start:start + 4], "little") in (2, 3):  # PT_DYNAMIC, PT_INTERP
            raise VerifyError(f"{name} is not statically linked")
    return architecture


def check_helpers(read, required: set[str]) -> list[str]:
    """`read(name)` returns the bytes of a file under keel/sandbox/bin, or None when it is missing."""
    raw = read("manifest.json")
    if raw is None:
        raise VerifyError("keel/sandbox/bin/manifest.json is missing")
    manifest = json.loads(raw)
    if manifest.get("version") != 1 or not isinstance(manifest.get("protocol"), int):
        raise VerifyError("the helper manifest has an unsupported version or protocol")
    artifacts = manifest.get("artifacts") or {}
    missing = required - set(artifacts)
    if missing:
        raise VerifyError("the helper manifest has no artifact for " + ", ".join(sorted(missing)))
    lines = []
    for architecture, entry in sorted(artifacts.items()):
        data = read(entry["file"])
        if data is None:
            raise VerifyError(f"the {architecture} helper {entry['file']} is missing")
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise VerifyError(f"the {architecture} helper does not match its manifest digest")
        if _elf_architecture(data, entry["file"]) != architecture:
            raise VerifyError(f"the {architecture} helper is built for another architecture")
        lines.append(f"{architecture}: {entry['file']} ({len(data)} bytes, protocol {manifest['protocol']})")
    return lines


def verify_wheel(path: Path, required: set[str]) -> list[str]:
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())

        def read(name):
            member = _BIN + name
            return wheel.read(member) if member in names else None

        return check_helpers(read, required)


def verify_sdist(path: Path) -> list[str]:
    with tarfile.open(path) as archive:
        members = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    missing = [name for name in _SDIST_MEMBERS if name not in members]
    if missing:
        raise VerifyError("the sdist is missing " + ", ".join(missing))
    return [f"sdist: {len(_SDIST_MEMBERS)} helper build and test sources present"]


def verify_installed(required: set[str]) -> list[str]:
    import platform

    from keel.sandbox.runtime import artifact_directory, helper_artifact

    directory = Path(artifact_directory())

    def read(name):
        path = directory / name
        return path.read_bytes() if path.is_file() else None

    lines = check_helpers(read, required)
    machine = platform.machine()
    artifact = helper_artifact(machine)  # what the coordinator selects on this machine (names are normalized)
    lines.append(f"installed: {machine} selects {Path(artifact.path).name}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=("wheel", "sdist", "installed"))
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--require", default="", help="comma-separated architectures that must be packaged")
    arguments = parser.parse_args(argv)
    required = {item for item in arguments.require.split(",") if item}
    try:
        if arguments.kind == "installed":
            lines = verify_installed(required)
        elif arguments.path is None:
            raise VerifyError(f"a {arguments.kind} path is required")
        elif arguments.kind == "wheel":
            lines = verify_wheel(arguments.path, required)
        else:
            lines = verify_sdist(arguments.path)
    except (VerifyError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
