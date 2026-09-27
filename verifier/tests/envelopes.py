"""Test helpers for DSSE-envelope receipt logs.

Editing a receipt means decoding an envelope's payload, changing it, and
re-encoding it without re-signing: exactly what someone holding the log
but not the private key can do.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from vouch_verifier.receipts import GENESIS
from vouch_verifier.signing import Keyring, load_keyring, pae

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "testdata" / "receipts_golden.jsonl"
GOLDEN_KEYS: Keyring = load_keyring([ROOT / "testdata" / "keys" / "golden.pub.pem"])
OTHER_KEYS: Keyring = load_keyring([ROOT / "testdata" / "keys" / "eval.pub.pem"])


def golden_lines() -> list[str]:
    return GOLDEN.read_text(encoding="utf-8").splitlines()


def edit_payload(line: str, edit: Callable[[str], str]) -> str:
    """Rewrite one envelope's payload text, keeping its signature."""
    envelope = json.loads(line)
    body = base64.b64decode(envelope["payload"]).decode("utf-8")
    envelope["payload"] = base64.b64encode(edit(body).encode("utf-8")).decode("ascii")
    return json.dumps(envelope)


def edit_body(line: str, edit: Callable[[dict[str, Any]], None]) -> str:
    """Rewrite one envelope's receipt body as JSON, keeping its signature."""

    def apply(body: str) -> str:
        tree = json.loads(body)
        edit(tree)
        return json.dumps(tree)

    return edit_payload(line, apply)


def write_log(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


GOLDEN_PRIVATE = ROOT / "testdata" / "keys" / "golden.pem"


def bodies(lines: list[str]) -> list[tuple[str, str]]:
    """(payloadType, body text) of each envelope line."""
    out = []
    for line in lines:
        envelope = json.loads(line)
        out.append((envelope["payloadType"], base64.b64decode(envelope["payload"]).decode("utf-8")))
    return out


def signed_chain(
    entries: list[tuple[str, dict[str, Any]]],
    overrides: dict[int, dict[str, Any]] | None = None,
) -> list[str]:
    """Sign entries with the golden key, linking each to the one before.

    This is what someone holding a trusted private key can produce: a
    log with valid signatures and, unless overrides replace fields of an
    entry after linking, a valid chain. It lets tests reach the checks
    that sit behind the chain, such as duplicate detection.
    """
    key = load_pem_private_key(GOLDEN_PRIVATE.read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    keyid = next(iter(GOLDEN_KEYS))
    head, lines = GENESIS, []
    for seq, (payload_type, body) in enumerate(entries):
        body = {**body, "seq": seq, "prev_digest": head, **(overrides or {}).get(seq, {})}
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = key.sign(pae(payload_type, payload))
        lines.append(
            json.dumps(
                {
                    "payload": base64.b64encode(payload).decode("ascii"),
                    "payloadType": payload_type,
                    "signatures": [{"keyid": keyid, "sig": base64.b64encode(sig).decode("ascii")}],
                }
            )
        )
        head = "sha256:" + hashlib.sha256(payload).hexdigest()
    return lines
