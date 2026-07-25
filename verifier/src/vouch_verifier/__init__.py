"""vouch verifier: claim extraction, fact matching, verdict assignment."""

from vouch_verifier.canonical import canonicalize
from vouch_verifier.verdict import Verdict, Tolerance, compare

__all__ = ["canonicalize", "Verdict", "Tolerance", "compare"]
