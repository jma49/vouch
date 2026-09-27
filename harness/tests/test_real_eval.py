"""Real-agent scoring: span alignment, detection counts, per-model rates."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vouch_harness.label import store
from vouch_harness.real_eval import MISSED, detection, main, score_runs, to_markdown
from vouch_verifier.matcher import DEFAULT_TOLERANCES

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "testdata" / "receipts_golden.jsonl"
KEY = b"vouch-golden-key"

# (run id, answer, {span text: human label})
RUNS = [
    (
        "m/t01/s0",
        "NVDA closed at 181.52. AMD last traded at 172.40.",
        {"181.52": "SUPPORTED", "172.40": "CONTRADICTED"},
    ),
    (
        "m/t01/s1",
        "NVDA closed at 181.52. NVDA volume was 41 million.",
        {"181.52": "SUPPORTED", "41 million": "UNSUPPORTED"},
    ),
    (
        "m/t02/s0",
        "AMD's P/E is 48. Its RSI is 99 as of July 24.",
        {"48": "UNSUPPORTED", "99": "UNSUPPORTED", "July 24": "NOT_A_CLAIM"},
    ),
    ("m/t02/s1", "AMD last traded at 172.04.", {}),  # unlabeled
]


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, dict[store.SpanKey, store.LabelRecord]]:
    runs = tmp_path / "runs"
    labels_path = tmp_path / "labels" / "alice.jsonl"
    for run_id, answer, human in RUNS:
        d = runs / run_id
        d.mkdir(parents=True)
        (d / "answer.txt").write_text(answer + "\n")
        (d / "meta.json").write_text(json.dumps({"prompt": "q"}))
        (d / "transcript.json").write_text("[]")
        shutil.copy(GOLDEN, d / "receipts.jsonl")
        for text, label in human.items():
            start = answer.index(text)
            store.append(
                labels_path,
                store.LabelRecord(
                    run=run_id,
                    start=start,
                    end=start + len(text),
                    text=text,
                    label=label,
                    labeler="alice",
                    at="2026-09-27T00:00:00+00:00",
                    source="token" if text != "July 24" else "manual",
                ),
            )
    return runs, store.load(labels_path)


def test_spans_align_and_missed_spans_count(
    workspace: tuple[Path, dict[store.SpanKey, store.LabelRecord]],
) -> None:
    runs, labels = workspace
    scores = {s.run: s for s in score_runs(runs, labels, DEFAULT_TOLERANCES, KEY)}
    assert scores["m/t01/s0"].pairs() == [
        ("SUPPORTED", "SUPPORTED"),
        ("CONTRADICTED", "CONTRADICTED"),
    ]
    # The date is not a token, so the verifier has no verdict for it.
    assert ("NOT_A_CLAIM", MISSED) in scores["m/t02/s0"].pairs()
    assert not scores["m/t02/s1"].labeled


def test_detection_counts_and_rates(
    workspace: tuple[Path, dict[store.SpanKey, store.LabelRecord]],
) -> None:
    runs, labels = workspace
    det = detection(score_runs(runs, labels, DEFAULT_TOLERANCES, KEY))
    # Flagged misreports: 172.40, 41 million, 99. Missed: the recalled
    # P/E, which the verifier leaves UNVERIFIABLE. NOT_A_CLAIM is excluded.
    assert (det.tp, det.fp, det.fn, det.tn) == (3, 0, 1, 2)
    assert det.precision is not None and det.precision.mean == 1.0
    assert det.recall is not None and det.recall.mean == pytest.approx(0.75)
    assert det.recall.ci_lo <= det.recall.mean <= det.recall.ci_hi


def test_report_separates_human_and_verifier_rates(
    workspace: tuple[Path, dict[store.SpanKey, store.LabelRecord]],
) -> None:
    runs, labels = workspace
    md = to_markdown(score_runs(runs, labels, DEFAULT_TOLERANCES, KEY), "alice", {}, labels)
    assert "| m | human labels | 3 |" in md  # only labeled runs
    assert "| m | verifier estimate | 4 |" in md  # every run
    assert "TP 3, FP 0, FN 1, TN 2" in md


def test_empty_labels_still_report(tmp_path: Path) -> None:
    (tmp_path / "runs").mkdir()
    md = to_markdown([], "alice", {}, {})
    assert "No labeled runs yet" in md


def test_cli(
    workspace: tuple[Path, dict[store.SpanKey, store.LabelRecord]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs, _ = workspace
    rc = main(
        [
            "--labeler",
            "alice",
            "--runs",
            str(runs),
            "--labels",
            str(runs.parent / "labels"),
            "--tolerances",
            str(ROOT / "tolerance.yaml"),
            "--key",
            KEY.decode(),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["labeled_runs"] == 3 and out["detection"]["tp"] == 3
