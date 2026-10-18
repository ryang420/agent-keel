from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from keel.sandbox.records import CleanupReceipt
from keel.sandbox.runtime import decode_receipt, encode_control


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
