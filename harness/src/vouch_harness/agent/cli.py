"""vouch-agent: run real models through the vouch proxy on the eval task set.

    vouch-agent --model gemini-flash [--samples 5] [--task ID ...] [--dry-run]

Each (model, task, sample) gets its own proxy session and run directory
under --out. Completed runs are skipped and every model response is
cached, so an interrupted or repeated invocation only pays for what it
has not done yet. Receipts are signed with --signing-key, else
$VOUCH_SIGNING_KEY, else the public evaluation key
(vouch_harness.signing), and each run's meta.json records the key id.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path

from vouch_harness.agent.llm import (
    CachedClient,
    LLMError,
    OpenAICompatClient,
    cache_identity,
    load_models,
)
from vouch_harness.agent.runner import RunSpec, execute, load_tasks, run_dir
from vouch_harness.signing import private_copy, resolve_signing_key

_TURNS_ESTIMATE = 3  # typical requests per run: tool calls, then the answer


def run_batch(pending: list[RunSpec], run_one: Callable[[RunSpec], Path], out: Path) -> int:
    """Run each spec; return how many failed. A failure of any kind is
    recorded in that run's error.txt and the batch moves on: one bad run
    must not cost the rest of a paid batch. The failed run has no
    meta.json, so a rerun retries it."""
    failed = 0
    for i, spec in enumerate(pending, start=1):
        try:
            d = run_one(spec)
        except Exception as e:
            failed += 1
            err = run_dir(out, spec)
            err.mkdir(parents=True, exist_ok=True)
            (err / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            print(
                f"[{i}/{len(pending)}] {spec.session}: FAILED: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            continue
        print(f"[{i}/{len(pending)}] {spec.session} -> {d}", file=sys.stderr)
    return failed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vouch-agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--model", required=True, help="model name from --models")
    p.add_argument("--models", type=Path, default=Path("eval/models.yaml"))
    p.add_argument("--tasks", type=Path, default=Path("eval/tasks.yaml"))
    p.add_argument("--samples", type=int, default=5, help="runs per task (default 5)")
    p.add_argument("--task", action="append", default=[], help="only this task id (repeatable)")
    p.add_argument("--out", type=Path, default=Path("eval/runs"))
    p.add_argument("--cache", type=Path, default=Path("eval/.cache"))
    p.add_argument("--proxy", type=Path, default=Path("proxy/bin/vouch"))
    p.add_argument("--schemas", type=Path, default=Path("schemas"))
    p.add_argument(
        "--signing-key",
        type=Path,
        default=None,
        help="Ed25519 private key PEM (default: $VOUCH_SIGNING_KEY, else the public eval key)",
    )
    p.add_argument(
        "--cite",
        action="store_true",
        help="citation condition: the proxy offers receipt citations and the model is asked to "
        "use them; runs go under <model>+cite",
    )
    p.add_argument("--dry-run", action="store_true", help="list pending runs; call nothing")
    args = p.parse_args(argv)

    models = load_models(args.models)
    if args.model not in models:
        p.error(f"unknown model {args.model!r}; configured: {', '.join(sorted(models))}")
    config = models[args.model]
    tasks = load_tasks(args.tasks)
    if args.task:
        unknown = set(args.task) - {t.id for t in tasks}
        if unknown:
            p.error(f"unknown task ids: {sorted(unknown)}")
        tasks = [t for t in tasks if t.id in args.task]

    specs = [RunSpec(args.model, t, s, cite=args.cite) for t in tasks for s in range(args.samples)]
    pending = [s for s in specs if not (run_dir(args.out, s) / "meta.json").exists()]
    print(
        f"vouch-agent: {config.name} ({config.model}): {len(specs)} runs, "
        f"{len(pending)} pending, ~{len(pending) * _TURNS_ESTIMATE} requests "
        f"at <= {config.rpm:g}/min",
        file=sys.stderr,
    )
    if args.dry_run or not pending:
        return 0
    if not args.proxy.exists():
        p.error(f"proxy binary {args.proxy} not found; run `make build`")

    try:
        inner = OpenAICompatClient(config)
    except LLMError as e:
        print(f"vouch-agent: {e}", file=sys.stderr)
        return 2
    client = CachedClient(inner, args.cache, identity=cache_identity(config))
    signing_key = resolve_signing_key(args.signing_key)
    with tempfile.TemporaryDirectory(prefix="vouch-agent-") as private_dir:
        # A 0600 copy that lives only for this batch (signing.private_copy).
        key_copy = private_copy(signing_key, Path(private_dir))
        env = {**os.environ, "VOUCH_SIGNING_KEY": str(key_copy)}
        failed = run_batch(
            pending,
            lambda spec: execute(spec, client, args.out, args.proxy, args.schemas, env),
            args.out,
        )
    print(
        f"vouch-agent: done; {failed} failed; cache {client.hits} hits, {client.misses} requests",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
