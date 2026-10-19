from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from keel.sandbox.base import SandboxError
from keel.sandbox.records import CleanupReceipt
from keel.sandbox import runtime
from keel.sandbox.runtime import HelperArtifact, decode_receipt, encode_control, helper_artifact


def _receipt_json(**overrides) -> bytes:
    value = {"version": 1, "exec_id": "exec-1", "control_token": uuid.uuid4().hex, "leader_status": 7,
             "control_closed": True, "descendants_complete": True, "uncertain_reason": None}
    value.update(overrides)
    return json.dumps(value).encode()


def test_control_frame_is_versioned_bounded_and_keeps_only_identity():
    control = uuid.uuid4().hex
    usage = uuid.uuid4().hex
    frame = encode_control(control, usage, cancel=True)

    assert frame == b"KEL1" + bytes.fromhex(control) + bytes.fromhex(usage) + b"\x01"
    assert len(frame) == 37


@pytest.mark.parametrize("control,usage", [("x" * 32, uuid.uuid4().hex), (uuid.uuid4().hex, "z" * 32)])
def test_control_frame_rejects_non_hex_tokens(control, usage):
    with pytest.raises(ValueError):
        encode_control(control, usage, cancel=False)


def test_receipt_decoder_accepts_only_bounded_versioned_identity_json():
    receipt = CleanupReceipt("exec-1", uuid.uuid4().hex, 7, True, True, None)
    encoded = json.dumps({"version": 1, "exec_id": receipt.exec_id, "control_token": receipt.control_token,
                          "leader_status": receipt.leader_status, "control_closed": True,
                          "descendants_complete": True, "uncertain_reason": None}).encode()

    assert decode_receipt(encoded) == receipt
    with pytest.raises(ValueError):
        decode_receipt(b'{"version":2}')
    with pytest.raises(ValueError):
        decode_receipt(b"x" * 5000)


def test_receipt_decoder_rejects_boolean_version_and_boolean_status():
    control = uuid.uuid4().hex
    base = {"version": 1, "exec_id": "exec-1", "control_token": control, "leader_status": 0,
            "control_closed": True, "descendants_complete": True, "uncertain_reason": None}
    with pytest.raises(ValueError):
        decode_receipt(json.dumps({**base, "version": True}).encode())
    with pytest.raises(ValueError):
        decode_receipt(json.dumps({**base, "leader_status": True}).encode())


def test_receipt_decoder_rejects_duplicate_fields_and_a_byte_order_mark():
    control = uuid.uuid4().hex
    duplicate = ('{"version":1,"exec_id":"exec-1","control_token":"%s","leader_status":0,"leader_status":null,'
                 '"control_closed":true,"descendants_complete":true,"uncertain_reason":null}'
                 % control).encode()
    with pytest.raises(ValueError, match="duplicate"):
        decode_receipt(duplicate)
    with pytest.raises(ValueError, match="byte-order mark"):
        decode_receipt(b"\xef\xbb\xbf" + _receipt_json())


def test_codec_import_does_not_require_the_store_package_first():
    """A cold import of the codecs must not hit the store package's import cycle with `records`."""
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(source)!r}); "
         "import keel.sandbox.runtime"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Helper artifact selection: local files and a deterministic manifest only.
# ---------------------------------------------------------------------------

_ARCHES = ("amd64", "arm64")


def _write_assets(directory: Path, *, arches=_ARCHES, protocol: int = 1, manifest_version: int = 1,
                  contents: dict[str, bytes] | None = None, mode: int = 0o755,
                  digests: dict[str, str] | None = None) -> dict[str, bytes]:
    """Synthetic byte fixtures; the default suite never builds a real helper."""
    bodies = contents or {arch: b"\x7fELF" + arch.encode() + b"-fixture" for arch in arches}
    artifacts = {}
    for arch, body in bodies.items():
        path = directory / f"keel-supervisor-{arch}"
        path.write_bytes(body)
        path.chmod(mode)
        digest = (digests or {}).get(arch, hashlib.sha256(body).hexdigest())
        artifacts[arch] = {"file": path.name, "sha256": digest}
    (directory / "manifest.json").write_text(
        json.dumps({"version": manifest_version, "protocol": protocol, "artifacts": artifacts}, sort_keys=True)
        + "\n")
    return bodies


@pytest.fixture
def asset_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "artifact_directory", lambda: tmp_path)
    return tmp_path


@pytest.mark.parametrize("architecture", ["amd64", "x86_64", "arm64", "aarch64", "ARM64"])
def test_helper_artifact_normalizes_the_daemon_architecture(asset_directory, architecture):
    bodies = _write_assets(asset_directory)

    artifact = helper_artifact(architecture)

    expected = "arm64" if architecture.lower() in ("arm64", "aarch64") else "amd64"
    assert isinstance(artifact, HelperArtifact)
    assert artifact.architecture == expected
    assert artifact.protocol == 1
    assert artifact.sha256 == hashlib.sha256(bodies[expected]).hexdigest()
    assert artifact.path == asset_directory / f"keel-supervisor-{expected}"


def test_helper_artifact_rejects_an_unsupported_architecture(asset_directory):
    _write_assets(asset_directory)
    with pytest.raises(SandboxError, match="architecture"):
        helper_artifact("s390x")


def test_helper_artifact_requires_the_manifest(asset_directory):
    _write_assets(asset_directory)
    (asset_directory / "manifest.json").unlink()
    with pytest.raises(SandboxError, match="manifest"):
        helper_artifact("amd64")


@pytest.mark.parametrize("manifest", [
    b"not json",
    b"[]",
    b'{"version":1}',
    json.dumps({"version": 2, "protocol": 1, "artifacts": {}}).encode(),
    json.dumps({"version": 1, "protocol": 2, "artifacts": {}}).encode(),
    json.dumps({"version": True, "protocol": 1, "artifacts": {}}).encode(),
    json.dumps({"version": 1, "protocol": 1, "artifacts": {"amd64": {"file": "../x", "sha256": "0" * 64}}}).encode(),
    json.dumps({"version": 1, "protocol": 1, "artifacts": {"amd64": {"file": "x", "sha256": "0" * 63}}}).encode(),
    json.dumps({"version": 1, "protocol": 1, "artifacts": {"amd64": {"file": "x"}}}).encode(),
    json.dumps({"version": 1, "protocol": 1, "artifacts": {"s390x": {"file": "x", "sha256": "0" * 64}}}).encode(),
])
def test_helper_artifact_rejects_an_invalid_manifest(asset_directory, manifest):
    _write_assets(asset_directory)
    (asset_directory / "manifest.json").write_bytes(manifest)
    with pytest.raises(SandboxError, match="manifest"):
        helper_artifact("amd64")


def test_helper_artifact_requires_the_requested_architecture(asset_directory):
    _write_assets(asset_directory, arches=("amd64",))
    with pytest.raises(SandboxError, match="arm64"):
        helper_artifact("arm64")


def test_helper_artifact_rejects_a_missing_file(asset_directory):
    _write_assets(asset_directory)
    (asset_directory / "keel-supervisor-arm64").unlink()
    with pytest.raises(SandboxError, match="missing"):
        helper_artifact("arm64")


def test_helper_artifact_rejects_a_digest_mismatch(asset_directory):
    _write_assets(asset_directory, digests={"amd64": "0" * 64})
    with pytest.raises(SandboxError, match="digest"):
        helper_artifact("amd64")


def test_helper_artifact_rejects_a_protocol_mismatch(asset_directory):
    _write_assets(asset_directory, protocol=2)
    with pytest.raises(SandboxError, match="protocol"):
        helper_artifact("amd64")


def test_helper_artifact_rejects_a_non_regular_or_non_executable_file(asset_directory):
    _write_assets(asset_directory, arches=("amd64",))
    # A directory named like the artifact is not a regular file.
    (asset_directory / "keel-supervisor-amd64").unlink()
    (asset_directory / "keel-supervisor-amd64").mkdir()
    with pytest.raises(SandboxError):
        helper_artifact("amd64")
    (asset_directory / "keel-supervisor-amd64").rmdir()
    _write_assets(asset_directory, arches=("amd64",), mode=0o644)
    with pytest.raises(SandboxError, match="executable"):
        helper_artifact("amd64")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_helper_artifact_rejects_an_unreadable_file(asset_directory):
    _write_assets(asset_directory, arches=("amd64",))
    path = asset_directory / "keel-supervisor-amd64"
    path.chmod(0o100)  # executable but not readable, so the digest cannot be taken
    with pytest.raises(SandboxError, match="could not be read"):
        helper_artifact("amd64")


def test_helper_artifact_selection_never_shells_out(asset_directory, monkeypatch):
    """Selection is a local file read; a build script is a separate, explicit step."""
    _write_assets(asset_directory)

    def forbidden(*args, **kwargs):
        raise AssertionError("artifact selection must not run a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert helper_artifact("amd64").architecture == "amd64"
