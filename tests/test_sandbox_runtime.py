from __future__ import annotations

import json
import uuid

import pytest

from keel.sandbox.records import CleanupReceipt
from keel.sandbox.runtime import decode_receipt, encode_control


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
