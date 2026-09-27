"""Ed25519 signatures in DSSE envelopes: the Python side of proxy/internal/sign.

Each receipt log line is a DSSE envelope whose signature covers the
exact payload bytes, through DSSE's pre-authentication encoding. So
verifying needs only base64 and Ed25519, never the canonicalizer
(docs/canonical-json.md), and anyone holding the public key can verify
without being able to forge (issue #53).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_public_key,
)

RECEIPT_PAYLOAD_TYPE = "application/vnd.vouch.receipt+json; version=4"
CHECKPOINT_PAYLOAD_TYPE = "application/vnd.vouch.checkpoint+json; version=2"

Keyring = dict[str, Ed25519PublicKey]


class SignatureError(ValueError):
    """An envelope is malformed or carries no valid trusted signature."""


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE pre-authentication encoding: the exact bytes that are signed."""
    head = f"DSSEv1 {len(payload_type.encode())} {payload_type} {len(payload)} "
    return head.encode() + payload


def key_id(key: Ed25519PublicKey) -> str:
    """Name a key: "ed25519:" plus the first 16 hex characters of sha256(raw key)."""
    raw = key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    key = load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError(f"{path}: not an Ed25519 public key")
    return key


def load_keyring(paths: list[str | Path]) -> Keyring:
    return {key_id(k): k for k in (load_public_key(p) for p in paths)}


def decode(envelope: dict[str, Any], payload_type: str) -> bytes:
    """Check an envelope's shape and type and return its payload,
    without verifying any signature."""
    if not isinstance(envelope, dict):
        raise SignatureError("line is not a DSSE envelope object")
    if envelope.get("payloadType") != payload_type:
        raise SignatureError(f"payload type {envelope.get('payloadType')!r}, want {payload_type!r}")
    payload = envelope.get("payload")
    if not isinstance(payload, str):
        raise SignatureError("envelope has no payload")
    try:
        return base64.b64decode(payload, validate=True)
    except binascii.Error as e:
        raise SignatureError(f"payload is not base64: {e}") from e


def open_envelope(envelope: dict[str, Any], payload_type: str, keys: Keyring) -> tuple[bytes, str]:
    """Verify an envelope and return (payload, id of the signing key).

    As in DSSE, one valid signature from a trusted key suffices, and
    signatures under unknown key ids are ignored (key rotation).
    """
    payload = decode(envelope, payload_type)
    message = pae(payload_type, payload)
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list):
        raise SignatureError("envelope has no signatures")
    for sig in signatures:
        if not isinstance(sig, dict):
            continue
        key = keys.get(str(sig.get("keyid")))
        if key is None:
            continue
        try:
            key.verify(base64.b64decode(str(sig.get("sig")), validate=True), message)
        except (InvalidSignature, binascii.Error):
            continue
        return payload, str(sig["keyid"])
    raise SignatureError("no valid signature from a trusted key")
