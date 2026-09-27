"""Human labels for claims in agent answers (docs/labeling.md).

Labels are append-only JSONL, one file per labeler under eval/labels/.
A correction is a new record for the same (run, span); the latest one
wins, and the history stays in the file. Nothing is ever rewritten, so
a label file is safe to commit mid-session and easy to review in a
diff.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# The six verdicts (design section 6.1), plus NOT_A_CLAIM for a span
# the tokenizer offered that is not a numeric claim at all.
LABELS = (
    "SUPPORTED",
    "CONTRADICTED",
    "UNSUPPORTED",
    "STALE",
    "DERIVED",
    "UNVERIFIABLE",
    "NOT_A_CLAIM",
)
# A cleared label: the labeler removed a span they had added by hand.
CLEARED = "CLEARED"

SpanKey = tuple[str, int, int]  # (run id, start, end)


@dataclass(frozen=True)
class LabelRecord:
    run: str  # "<model>/<task>/<sample>", relative to the runs directory
    start: int
    end: int
    text: str
    label: str
    labeler: str
    at: str  # ISO 8601 UTC
    source: str = "token"  # "token" (offered by the tokenizer) or "manual"
    note: str = ""

    @property
    def key(self) -> SpanKey:
        return (self.run, self.start, self.end)


def append(path: Path, record: LabelRecord) -> None:
    if record.label not in (*LABELS, CLEARED):
        raise ValueError(f"unknown label {record.label!r}")
    if not 0 <= record.start < record.end:
        raise ValueError(f"bad span {record.start}..{record.end}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")


def load(path: Path) -> dict[SpanKey, LabelRecord]:
    """Current label per span: the latest record wins; cleared spans drop out."""
    current: dict[SpanKey, LabelRecord] = {}
    if not path.exists():
        return current
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                record = LabelRecord(**json.loads(line))
            except (TypeError, json.JSONDecodeError) as e:
                raise ValueError(f"{path}:{lineno}: {e}") from e
            if record.label == CLEARED:
                current.pop(record.key, None)
            else:
                current[record.key] = record
    return current
