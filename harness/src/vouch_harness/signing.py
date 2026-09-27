"""The receipt signing key for real-agent runs, resolved in one place.

vouch-agent signs runs with this key (through the proxy's
VOUCH_HMAC_KEY) and vouch-eval-real verifies them with it. Both must
resolve it the same way, or a key exported for another make target
signs runs that scoring then rejects. The default is a fixed, public
evaluation key: these receipts prove integrity of the published eval
data, not secrecy.
"""

from __future__ import annotations

import hashlib
import os

EVAL_HMAC_KEY = "vouch-eval-key"


def resolve_key(explicit: str | None = None) -> str:
    """An explicit key, else $VOUCH_HMAC_KEY, else the public eval key."""
    if explicit is not None:
        return explicit
    return os.environ.get("VOUCH_HMAC_KEY") or EVAL_HMAC_KEY


def key_id(key: str | bytes) -> str:
    """A short, non-secret fingerprint of a key, recorded per run so a
    mismatch is diagnosable without revealing the key."""
    raw = key.encode("utf-8") if isinstance(key, str) else key
    return hashlib.sha256(raw).hexdigest()[:8]
