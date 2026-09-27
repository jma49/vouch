"""Read agent run directories into what a labeler needs to see.

A labeler judges each numeric span against the evidence: the facts the
proxy extracted and the raw tool results. What they must *not* see is
the verifier's verdict (docs/labeling.md: blind labeling), so nothing
here imports the matcher.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vouch_verifier.receipts import load_log
from vouch_verifier.tokens import tokenize


@dataclass(frozen=True)
class RunView:
    id: str
    prompt: str
    answer: str
    finished: bool
    spans: list[dict[str, Any]]  # offered numeric spans: start, end, text
    facts: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]  # name, arguments, result text

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "answer": self.answer,
            "finished": self.finished,
            "spans": self.spans,
            "facts": self.facts,
            "tool_calls": self.tool_calls,
        }


def discover(runs_dir: Path) -> list[str]:
    """Run ids ("model/task/sample") of every completed run, sorted."""
    return sorted(
        str(meta.parent.relative_to(runs_dir)) for meta in runs_dir.glob("*/*/*/meta.json")
    )


def _tool_calls(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = {
        m.get("tool_call_id"): m.get("content", "") for m in transcript if m.get("role") == "tool"
    }
    calls = []
    for m in transcript:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            calls.append(
                {
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                    "result": results.get(tc.get("id"), ""),
                }
            )
    return calls


def load_run(runs_dir: Path, run_id: str) -> RunView:
    d = runs_dir / run_id
    if d.resolve().parent.parent.parent != runs_dir.resolve():
        raise ValueError(f"not a run id: {run_id!r}")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    answer = (d / "answer.txt").read_text(encoding="utf-8").rstrip("\n")
    receipts = load_log(d / "receipts.jsonl") if (d / "receipts.jsonl").exists() else []
    transcript = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
    return RunView(
        id=run_id,
        prompt=meta["prompt"],
        answer=answer,
        finished=bool(meta.get("finished", True)),
        spans=[{"start": t.start, "end": t.end, "text": t.text} for t in tokenize(answer)],
        facts=[
            {
                "receipt": r.receipt_id[:8],
                "tool": r.tool_name,
                "entity": f.entity,
                "metric": f.metric,
                "value": f.value,
                "as_of": f.as_of or r.data_asof or "",
            }
            for r in receipts
            for f in r.facts
        ],
        tool_calls=_tool_calls(transcript),
    )
