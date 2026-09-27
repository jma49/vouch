"""README regeneration: markers, staleness check, determinism."""

from pathlib import Path

import pytest

from vouch_harness.readme import main, regenerate, render_latency, splice

ROOT = Path(__file__).resolve().parents[2]

SKELETON = """# title

<!-- BEGIN GENERATED eval-metrics: do not edit -->
stale metrics
<!-- END GENERATED eval-metrics -->

prose stays

<!-- BEGIN GENERATED example-report -->
stale example
<!-- END GENERATED example-report -->

<!-- BEGIN GENERATED latency -->
stale latency
<!-- END GENERATED latency -->
"""


def test_splice_replaces_only_the_named_block() -> None:
    out = splice(SKELETON, "eval-metrics", "fresh\n")
    assert "fresh\n<!-- END GENERATED eval-metrics -->" in out
    assert "stale metrics" not in out
    assert "stale example" in out
    assert "prose stays" in out


def test_splice_rejects_missing_block() -> None:
    with pytest.raises(ValueError, match="no generated block"):
        splice("# no markers\n", "eval-metrics", "x\n")


def test_regenerate_is_deterministic_and_idempotent() -> None:
    once = regenerate(SKELETON, ROOT)
    assert regenerate(SKELETON, ROOT) == once
    assert regenerate(once, ROOT) == once
    assert "| Mutation detection rate |" in once
    assert "**CONTRADICTED**" in once
    assert "| Agent to upstream, through vouch |" in once


def test_render_latency_picks_units_and_names_the_machine() -> None:
    d = {"p50_us": 6.583, "p99_us": 14.4}
    out = render_latency(
        {"calls": 10, "machine": "", "goos": "linux", "goarch": "amd64", "cpus": 4, "go": "go1.22",
         "direct": d, "proxied": {"p50_us": 4894.917, "p99_us": 12000.0}, "append": d}
    )  # fmt: skip
    assert "unknown CPU (linux/amd64, 4 CPUs, go1.22)" in out
    assert "| Agent to upstream, direct | 6.6 µs | 14.4 µs |" in out
    assert "| Agent to upstream, through vouch | 4.89 ms | 12.00 ms |" in out


def test_check_flags_stale_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for rel in (
        "testdata/receipts_golden.jsonl",
        "testdata/keys/golden.pub.pem",
        "tolerance.yaml",
        "examples/answer.txt",
        "docs/bench/latency.json",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    readme = tmp_path / "README.md"
    readme.write_text(SKELETON, encoding="utf-8")

    assert main([str(readme), "--check"]) == 1
    assert "stale" in capsys.readouterr().err
    assert main([str(readme)]) == 0
    assert main([str(readme), "--check"]) == 0


def test_repo_readme_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main([str(ROOT / "README.md"), "--check"]) == 0, "run `make readme`"
