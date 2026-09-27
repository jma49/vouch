"""README regeneration: markers, staleness check, determinism."""

from pathlib import Path

import pytest

from vouch_harness.readme import main, regenerate, splice

ROOT = Path(__file__).resolve().parents[2]
KEY = b"vouch-golden-key"

SKELETON = """# title

<!-- BEGIN GENERATED eval-metrics: do not edit -->
stale metrics
<!-- END GENERATED eval-metrics -->

prose stays

<!-- BEGIN GENERATED example-report -->
stale example
<!-- END GENERATED example-report -->
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
    once = regenerate(SKELETON, ROOT, KEY)
    assert regenerate(SKELETON, ROOT, KEY) == once
    assert regenerate(once, ROOT, KEY) == once
    assert "| Mutation detection rate |" in once
    assert "**CONTRADICTED**" in once


def test_check_flags_stale_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOUCH_HMAC_KEY", KEY.decode())
    for rel in ("testdata/receipts_golden.jsonl", "tolerance.yaml", "examples/answer.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    readme = tmp_path / "README.md"
    readme.write_text(SKELETON, encoding="utf-8")

    assert main([str(readme), "--check"]) == 1
    assert "stale" in capsys.readouterr().err
    assert main([str(readme)]) == 0
    assert main([str(readme), "--check"]) == 0


def test_repo_readme_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_HMAC_KEY", KEY.decode())
    assert main([str(ROOT / "README.md"), "--check"]) == 0, "run `make readme`"
