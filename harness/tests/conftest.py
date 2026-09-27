"""Shared test settings for the harness."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROXY = Path(__file__).resolve().parents[2] / "proxy" / "bin" / "vouch"

# End-to-end tests drive the real Go proxy. Without a built binary they
# skip, except where $VOUCH_REQUIRE_PROXY is set (CI's job with both
# toolchains): there a missing binary fails them, so the tests that
# prove the second domain and the citation channel cannot quietly never
# run (#106).
needs_proxy = pytest.mark.skipif(
    not PROXY.exists() and not os.environ.get("VOUCH_REQUIRE_PROXY"),
    reason="proxy binary not built (make build)",
)
