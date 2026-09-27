"""vouch verifier: claim extraction, fact matching, verdict assignment."""

from vouch_verifier.canonical import canonicalize
from vouch_verifier.verdict import Tolerance, Verdict, compare

__all__ = ["Tolerance", "Verdict", "canonicalize", "compare"]
