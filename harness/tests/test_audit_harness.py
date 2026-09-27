"""Regressions from the 2026-09-27 audit, #104: silent data loss in
scoring, tracebacks and silent bad settings, and untested paths."""

from __future__ import annotations

import json
import random
import shutil
import stat
import sys
from pathlib import Path

import pytest
from keys import GOLDEN_KEYS

from vouch_harness import cli as eval_cli
from vouch_harness.agent import cli as agent_cli
from vouch_harness.agent import runner
from vouch_harness.agent.llm import Reply, load_models
from vouch_harness.mutate import _sign_flip
from vouch_harness.real_eval import main as eval_real
from vouch_harness.real_eval import score_runs
from vouch_verifier.claims import extract_claims
from vouch_verifier.matcher import DEFAULT_TOLERANCES
from vouch_verifier.receipts import ReceiptError

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "testdata" / "receipts_golden.jsonl"


def _run(runs: Path, run_id: str, head: str | None, log: bool) -> None:
    d = runs / run_id
    d.mkdir(parents=True)
    (d / "answer.txt").write_text("NVDA closed at 181.52.\n")
    (d / "meta.json").write_text(json.dumps({"prompt": "q", "head": head}))
    if log:
        shutil.copy(GOLDEN, d / "receipts.jsonl")


def test_a_deleted_log_is_an_error_not_silence(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "m/t01/s0", head="sha256:00", log=False)
    with pytest.raises(ReceiptError, match="missing"):
        score_runs(runs, {}, DEFAULT_TOLERANCES, GOLDEN_KEYS)
    # A run that never had a log (no head) is still fine.
    shutil.rmtree(runs)
    _run(runs, "m/t01/s0", head=None, log=False)
    assert score_runs(runs, {}, DEFAULT_TOLERANCES, GOLDEN_KEYS)[0].verifier


def test_an_unknown_labeler_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs, labels = tmp_path / "runs", tmp_path / "labels"
    runs.mkdir()
    labels.mkdir()
    (labels / "alice.jsonl").write_text("")
    key = str(ROOT / "testdata" / "keys" / "eval.pub.pem")
    args = ["--runs", str(runs), "--labels", str(labels), "--public-key", key,
            "--tolerances", str(ROOT / "tolerance.yaml"), "--labeler"]  # fmt: skip
    assert eval_real([*args, "alcie"]) == 2
    assert "alice" in capsys.readouterr().err
    assert eval_real([*args, "alice"]) == 0


def test_vouch_eval_reports_a_missing_policy_with_exit_2(tmp_path: Path) -> None:
    assert (
        eval_cli.main(["--receipts", str(GOLDEN), "--tolerances", str(tmp_path / "no.yaml")]) == 2
    )


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"base_url": "u", "model": "m", "api_key_env": "K", "rpm": 0}, "rpm"),
        ({"base_url": "u", "model": "m"}, "missing keys"),
        ("not a mapping", "mapping"),
    ],
)
def test_bad_model_entries_are_value_errors(tmp_path: Path, spec: object, message: str) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(json.dumps({"m": spec}))
    with pytest.raises(ValueError, match=message):
        load_models(path)


def test_agent_cli_plans_resumes_and_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = ["--model", "gemini-flash", "--models", str(ROOT / "eval" / "models.yaml"),
            "--tasks", str(ROOT / "eval" / "tasks.yaml"), "--out", str(tmp_path)]  # fmt: skip
    with pytest.raises(SystemExit):
        agent_cli.main([*base, "--samples", "0"])
    with pytest.raises(SystemExit):
        agent_cli.main([*base, "--task", "no-such-task"])
    task = runner.load_tasks(ROOT / "eval" / "tasks.yaml")[0].id
    assert agent_cli.main([*base, "--task", task, "--samples", "2", "--dry-run"]) == 0
    assert "2 runs, 2 pending" in capsys.readouterr().err
    done = tmp_path / "gemini-flash" / task / "s0"
    done.mkdir(parents=True)
    (done / "meta.json").write_text("{}")
    assert agent_cli.main([*base, "--task", task, "--samples", "2", "--dry-run"]) == 0
    assert "2 runs, 1 pending" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # a missing proxy binary, before any request
        agent_cli.main([*base, "--task", task, "--proxy", str(tmp_path / "none")])


# Answers MCP like a proxy, then exits 3 at EOF: a proxy that failed.
_FAILING_PROXY = """#!{python}
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    result = {{"tools": []}} if req["method"] == "tools/list" else {{}}
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": req["id"], "result": result}}) + "\\n")
    sys.stdout.flush()
sys.exit(3)
"""


def test_a_proxy_that_fails_leaves_no_complete_run(tmp_path: Path) -> None:
    fake = tmp_path / "vouch"
    fake.write_text(_FAILING_PROXY.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    class Answers:
        def complete(self, messages: object, tools: object, sample: int) -> Reply:
            return Reply({"role": "assistant", "content": "Fine."}, "stop")

    spec = runner.RunSpec("fake", runner.Task("t01", "q"), sample=0)
    with pytest.raises(RuntimeError, match="status 3"):
        runner.execute(spec, Answers(), tmp_path / "out", fake, ROOT / "schemas", {})
    assert not (runner.run_dir(tmp_path / "out", spec) / "meta.json").exists()


def test_sign_flip_by_direction_word() -> None:
    answer = "AMD fell 1.35% today."
    mutant = _sign_flip(answer, list(extract_claims(answer, {"AMD"}).claims), random.Random(0))
    assert mutant is not None and mutant.answer == "AMD rose 1.35% today."
