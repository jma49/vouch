"""Test helpers for DSSE-envelope receipt logs.

Editing a receipt means decoding an envelope's payload, changing it, and
re-encoding it without re-signing: exactly what someone holding the log
but not the private key can do.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vouch_verifier.signing import Keyring, load_keyring

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
