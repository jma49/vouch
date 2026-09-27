"""Signing keys for real-agent runs, resolved in one place (#35).

vouch-agent has the proxy sign every run with an Ed25519 private key,
and vouch-eval-real verifies runs with the matching public keys. Both
resolve keys here, so a key chosen for one command is the key the other
expects. The defaults are the committed, public evaluation keypair
(testdata/keys/README.md): those receipts prove the integrity of the
published eval data, not who produced it.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from vouch_verifier.signing import key_id

EVAL_SIGNING_KEY = Path("testdata/keys/eval.pem")
EVAL_PUBLIC_KEY = Path("testdata/keys/eval.pub.pem")


def resolve_signing_key(explicit: Path | None = None) -> Path:
    """An explicit key, else $VOUCH_SIGNING_KEY, else the eval key."""
    if explicit is not None:
        return explicit
    env = os.environ.get("VOUCH_SIGNING_KEY")
    return Path(env) if env else EVAL_SIGNING_KEY


def resolve_public_keys(explicit: list[Path] | None = None) -> list[Path]:
    """Explicit keys, else $VOUCH_PUBLIC_KEY (os.pathsep-separated),
    else the eval public key."""
    if explicit:
        return explicit
    env = [Path(p) for p in os.environ.get("VOUCH_PUBLIC_KEY", "").split(os.pathsep) if p]
    return env or [EVAL_PUBLIC_KEY]


def signing_key_id(path: Path) -> str:
    """The key id of a private key file, recorded per run so a run signed
    with an unexpected key is diagnosable without the key itself."""
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path}: not an Ed25519 private key")
    return key_id(key.public_key())


def private_copy(key: Path, directory: Path) -> Path:
    """Copy a private key into directory with mode 0600.

    The proxy refuses private keys other users can read, as ssh does, and
    git does not preserve file modes, so the committed eval key arrives
    world-readable after a checkout.
    """
    target = directory / "signing.pem"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.read_bytes())
    return target
