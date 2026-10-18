"""ELF verification in the helper build script; no Docker daemon or container is needed."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import struct

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("build_supervisor", REPO_ROOT / "scripts" / "build_supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_supervisor = _load()


def _elf(*, machine: int = 62, kind: int = 2, program_offset: int = 64, entry_size: int = 56, count: int = 1,
         segments: list[int] | None = None, size: int | None = None) -> bytes:
    """A minimal ELF64 little-endian header with the given program header table."""
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4], header[5] = 2, 1
    struct.pack_into("<H", header, 16, kind)
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, program_offset)
    struct.pack_into("<H", header, 54, entry_size)
    struct.pack_into("<H", header, 56, count)
    data = bytearray(header)
    for index, segment in enumerate(segments if segments is not None else [1] * count):
        start = program_offset + index * entry_size
        if len(data) < start + entry_size:
            data.extend(b"\0" * (start + entry_size - len(data)))
        struct.pack_into("<I", data, start, segment)
    return bytes(data if size is None else data[:size])


def _write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "keel-supervisor"
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def test_elf_details_accepts_static_amd64_and_arm64_binaries(tmp_path):
    assert build_supervisor.elf_details(_write(tmp_path, _elf())) == ("amd64", [])
    assert build_supervisor.elf_details(_write(tmp_path, _elf(machine=183))) == ("arm64", [])


def test_elf_details_rejects_a_short_program_header(tmp_path):
    # The table says one 56-byte header follows the 64-byte ELF header, but only four bytes are present.
    with pytest.raises(build_supervisor.BuildError, match="truncated"):
        build_supervisor.elf_details(_write(tmp_path, _elf(size=68)))


def test_elf_details_rejects_a_truncated_program_header_table(tmp_path):
    # Two headers are declared and only one is present.
    with pytest.raises(build_supervisor.BuildError, match="truncated"):
        build_supervisor.elf_details(_write(tmp_path, _elf(count=2, segments=[1])))


def test_elf_details_reports_dynamic_linking(tmp_path):
    path = _write(tmp_path, _elf(segments=[3]))
    assert build_supervisor.elf_details(path) == ("amd64", ["it requests a dynamic loader (PT_INTERP)"])
    path = _write(tmp_path, _elf(segments=[2]))
    assert build_supervisor.elf_details(path) == ("amd64", ["it has a dynamic section (PT_DYNAMIC)"])


def test_elf_details_rejects_an_unsupported_machine(tmp_path):
    with pytest.raises(build_supervisor.BuildError, match="machine"):
        build_supervisor.elf_details(_write(tmp_path, _elf(machine=3)))
