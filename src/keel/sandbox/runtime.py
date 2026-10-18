"""Wire codecs for the in-container supervisor control and receipt protocol.

The control frame is exactly ``KEL1`` + 16 raw bytes of the 32-hex control token + 16 raw bytes of the 32-hex
usage token + one cancel byte (37 bytes). Terminal receipts are bounded JSON with identity and status only; payload
output is a separate transport and must never be parsed as a receipt.
"""

from __future__ import annotations

import json
import re

from keel.sandbox.records import CleanupReceipt

_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_EXEC_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REASON = re.compile(r"^[a-z0-9_]{1,512}$")
_MAX_RECEIPT_BYTES = 4096
_PROTOCOL_VERSION = 1
_RECEIPT_FIELDS = frozenset(
    {"version", "exec_id", "control_token", "leader_status", "control_closed", "descendants_complete",
     "uncertain_reason"})
_BYTE_ORDER_MARK = b"\xef\xbb\xbf"


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Reject duplicate JSON members; `json.loads` would silently keep the last value."""
    value: dict = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("receipt has duplicate fields")
        value[key] = member
    return value


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
