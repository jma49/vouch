"""vouch-agent: run real models through the vouch proxy on the eval task set.

    vouch-agent --model gemini-flash [--samples 5] [--task ID ...] [--dry-run]

Each (model, task, sample) gets its own proxy session and run directory
under --out. Completed runs are skipped and every model response is
cached, so an interrupted or repeated invocation only pays for what it
has not done yet. The receipt signing key defaults to a fixed, public
evaluation key: these receipts prove integrity of the published eval
data, not secrecy.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from vouch_harness.agent.llm import CachedClient, LLMError, OpenAICompatClient, load_models
from vouch_harness.agent.runner import RunSpec, execute, load_tasks, run_dir

EVAL_HMAC_KEY = "vouch-eval-key"
_TURNS_ESTIMATE = 3  # typical requests per run: tool calls, then the answer


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

    specs = [RunSpec(args.model, t, s) for t in tasks for s in range(args.samples)]
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
    client = CachedClient(inner, args.cache, identity=f"{config.endpoint}|{config.model}")
    env = {**os.environ, "VOUCH_HMAC_KEY": os.environ.get("VOUCH_HMAC_KEY", EVAL_HMAC_KEY)}

    failed = 0
    for i, spec in enumerate(pending, start=1):
        try:
            d = execute(spec, client, args.out, args.proxy, args.schemas, env)
        except LLMError as e:
            # Keep going: the completed runs are saved, and a rerun resumes.
            failed += 1
            print(f"[{i}/{len(pending)}] {spec.session}: FAILED: {e}", file=sys.stderr)
            continue
        print(f"[{i}/{len(pending)}] {spec.session} -> {d}", file=sys.stderr)
    print(
        f"vouch-agent: done; {failed} failed; cache {client.hits} hits, {client.misses} requests",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
