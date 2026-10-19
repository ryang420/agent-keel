"""Wire codecs for the in-container supervisor control and receipt protocol, and helper artifact selection.

The control frame is exactly ``KEL1`` + 16 raw bytes of the 32-hex control token + 16 raw bytes of the 32-hex
usage token + one cancel byte (37 bytes). Terminal receipts are bounded JSON with identity and status only; payload
output is a separate transport and must never be parsed as a receipt.

Helper artifacts are selected from the installed package only. A deterministic ``manifest.json`` names each
architecture's file and SHA-256; selection reads local files, never the network, and never runs a process.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from keel.sandbox.base import SandboxError
from keel.sandbox.records import CleanupReceipt

_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_EXEC_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REASON = re.compile(r"^[a-z0-9_]{1,512}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 4096
_PROTOCOL_VERSION = 1
_RECEIPT_FIELDS = frozenset(
    {"version", "exec_id", "control_token", "leader_status", "control_closed", "descendants_complete",
     "uncertain_reason"})
_BYTE_ORDER_MARK = b"\xef\xbb\xbf"

HELPER_PROTOCOL_VERSION = 1
HELPER_MANIFEST_VERSION = 1
HELPER_MANIFEST_NAME = "manifest.json"
_ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_MANIFEST_FIELDS = frozenset({"version", "protocol", "artifacts"})
_ARTIFACT_FIELDS = frozenset({"file", "sha256"})
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Reject duplicate JSON members; `json.loads` would silently keep the last value."""
    value: dict = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("document has duplicate fields")
        value[key] = member
    return value


@dataclass(frozen=True)
class HelperArtifact:
    """One verified local helper binary and the manifest facts that identify it."""

    path: Path
    sha256: str
    protocol: int
    architecture: str


def artifact_directory() -> Path:
    """Where this installation stores its helper artifacts; tests substitute a synthetic directory."""
    return Path(__file__).resolve().parent / "bin"


def normalize_architecture(architecture: str) -> str:
    """Map Docker's daemon architecture spellings onto the two names keel ships."""
    if not isinstance(architecture, str):
        raise SandboxError("the Docker architecture must be a string")
    normalized = _ARCHITECTURES.get(architecture.strip().lower())
    if normalized is None:
        raise SandboxError(f"unsupported Docker architecture {architecture!r}; keel supports amd64 and arm64")
    return normalized


def _manifest_error(directory: Path, reason: str) -> SandboxError:
    return SandboxError(f"the sandbox helper manifest in {directory} is invalid: {reason}")


def _read_manifest(directory: Path) -> tuple[int, dict[str, dict[str, str]]]:
    path = directory / HELPER_MANIFEST_NAME
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SandboxError(f"the sandbox helper manifest is missing: {path}") from exc
    if not data or len(data) > _MAX_MANIFEST_BYTES or data.startswith(_BYTE_ORDER_MARK):
        raise _manifest_error(directory, "it is empty, oversized or starts with a byte-order mark")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _manifest_error(directory, "it is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise _manifest_error(directory, "it does not have exactly version, protocol and artifacts")
    version, protocol, artifacts = value["version"], value["protocol"], value["artifacts"]
    if type(version) is not int or version != HELPER_MANIFEST_VERSION:
        raise _manifest_error(directory, "it has an unsupported version")
    if type(protocol) is not int or protocol != HELPER_PROTOCOL_VERSION:
        raise _manifest_error(directory, f"it declares an unsupported protocol {protocol!r}")
    if not isinstance(artifacts, dict):
        raise _manifest_error(directory, "artifacts is not an object")
    entries: dict[str, dict[str, str]] = {}
    for architecture, entry in artifacts.items():
        if architecture not in ("amd64", "arm64"):
            raise _manifest_error(directory, f"it names an unsupported architecture {architecture!r}")
        if not isinstance(entry, dict) or set(entry) != _ARTIFACT_FIELDS:
            raise _manifest_error(directory, f"the {architecture} entry is invalid")
        name, digest = entry["file"], entry["sha256"]
        if (not isinstance(name, str) or not name or name in (".", "..") or len(name) > 255
                or "/" in name or "\\" in name or "\0" in name):
            raise _manifest_error(directory, f"the {architecture} file name is invalid")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise _manifest_error(directory, f"the {architecture} digest is invalid")
        entries[architecture] = {"file": name, "sha256": digest}
    return protocol, entries


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise SandboxError(f"the sandbox helper artifact could not be read: {path}") from exc
    return digest.hexdigest()


def helper_artifact(architecture: str) -> HelperArtifact:
    """Select and verify the local helper for a daemon architecture (``amd64`` or ``arm64``)."""
    normalized = normalize_architecture(architecture)
    directory = artifact_directory()
    protocol, artifacts = _read_manifest(directory)
    entry = artifacts.get(normalized)
    if entry is None:
        raise SandboxError(f"the sandbox helper manifest has no {normalized} artifact")
    path = directory / entry["file"]
    real_directory = Path(os.path.realpath(directory))
    if Path(os.path.realpath(path)).parent != real_directory:
        raise _manifest_error(directory, f"the {normalized} file is outside the artifact directory")
    try:
        status = path.stat()
    except OSError as exc:
        raise SandboxError(f"the sandbox helper artifact is missing: {path}") from exc
    if not path.is_file():
        raise SandboxError(f"the sandbox helper artifact is not a regular file: {path}")
    if not os.access(path, os.X_OK) or not status.st_mode & 0o111:
        raise SandboxError(f"the sandbox helper artifact is not executable: {path}")
    if status.st_size > _MAX_ARTIFACT_BYTES:
        raise SandboxError(f"the sandbox helper artifact is unexpectedly large: {path}")
    digest = _file_digest(path)
    if digest != entry["sha256"]:
        raise SandboxError(f"the sandbox helper artifact does not match its manifest digest: {path}")
    return HelperArtifact(path, digest, protocol, normalized)


def encode_control(control_token: str, usage_token: str, *, cancel: bool) -> bytes:
    """Encode the fixed version-1 binary control frame; it contains no payload or process identifier."""
    if not isinstance(control_token, str) or not _TOKEN.fullmatch(control_token):
        raise ValueError("control token must be 32 lowercase hexadecimal characters")
    if not isinstance(usage_token, str) or not _TOKEN.fullmatch(usage_token):
        raise ValueError("usage token must be 32 lowercase hexadecimal characters")
    if not isinstance(cancel, bool):
        raise ValueError("cancel must be a boolean")
    return b"KEL1" + bytes.fromhex(control_token) + bytes.fromhex(usage_token) + bytes((int(cancel),))


def decode_receipt(data: bytes) -> CleanupReceipt:
    """Decode a bounded, identity-only JSON receipt emitted separately from payload stdout."""
    if not isinstance(data, bytes) or not data or len(data) > _MAX_RECEIPT_BYTES:
        raise ValueError("receipt must be a non-empty bounded byte string")
    if data.startswith(_BYTE_ORDER_MARK):
        raise ValueError("receipt must not start with a byte-order mark")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
        raise ValueError("receipt has unsupported fields")
    version = value["version"]
    if type(version) is not int or version != _PROTOCOL_VERSION:
        raise ValueError("receipt has an unsupported version")
    if not isinstance(value["exec_id"], str) or not _EXEC_ID.fullmatch(value["exec_id"]):
        raise ValueError("receipt exec identity is invalid")
    if not isinstance(value["control_token"], str) or not _TOKEN.fullmatch(value["control_token"]):
        raise ValueError("receipt control token is invalid")
    status = value["leader_status"]
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)
                               or not 0 <= status <= 255):
        raise ValueError("receipt leader status is invalid")
    for field in ("control_closed", "descendants_complete"):
        if not isinstance(value[field], bool):
            raise ValueError(f"receipt {field} must be a boolean")
    reason = value["uncertain_reason"]
    if reason is not None and (not isinstance(reason, str) or not _REASON.fullmatch(reason)):
        raise ValueError("receipt uncertainty reason is invalid")
    return CleanupReceipt(value["exec_id"], value["control_token"], status,
                          value["control_closed"], value["descendants_complete"], reason)
