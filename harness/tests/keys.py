"""The committed test keys (testdata/keys/README.md), for harness tests."""

from __future__ import annotations

from pathlib import Path

from vouch_harness.signing import private_copy
from vouch_verifier.signing import Keyring, load_keyring

ROOT = Path(__file__).resolve().parents[2]
KEYS_DIR = ROOT / "testdata" / "keys"
GOLDEN_PUB = KEYS_DIR / "golden.pub.pem"
EVAL_PUB = KEYS_DIR / "eval.pub.pem"
EVAL_PRIV = KEYS_DIR / "eval.pem"
GOLDEN_KEYS: Keyring = load_keyring([GOLDEN_PUB])
EVAL_KEYS: Keyring = load_keyring([EVAL_PUB])
GOLDEN_KEY_ID = next(iter(GOLDEN_KEYS))
EVAL_KEY_ID = next(iter(EVAL_KEYS))


def proxy_env(tmp: Path) -> dict[str, str]:
    """An environment in which the proxy signs with the eval key."""
    private = tmp / "private"
    private.mkdir()
    return {"VOUCH_SIGNING_KEY": str(private_copy(EVAL_PRIV, private)), "PATH": "/usr/bin:/bin"}
