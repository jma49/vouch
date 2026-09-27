"""vouch-label: a local web tool for labeling claims in agent answers.

    vouch-label serve --labeler NAME [--runs eval/runs] [--port 8765]
    vouch-label agreement NAME_A NAME_B
    vouch-label stats

Binds to 127.0.0.1 only. Labels are blind: the page shows the answer,
the receipted facts, and the raw tool results, never the verifier's
verdict. See docs/labeling.md for what each label means.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from vouch_harness.label import store
from vouch_harness.label.agreement import agreement
from vouch_harness.label.runs import discover, load_run

_LABELER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class LabelApp:
    """Request handling, independent of the HTTP server for testing."""

    def __init__(self, runs_dir: Path, labels_dir: Path, labeler: str) -> None:
        if not _LABELER_RE.match(labeler):
            raise ValueError("labeler must be lowercase letters, digits, - or _")
        self.runs_dir = runs_dir
        self.labeler = labeler
        self.path = labels_dir / f"{labeler}.jsonl"

    def runs(self) -> list[dict[str, Any]]:
        labels = store.load(self.path)
        out = []
        for run_id in discover(self.runs_dir):
            view = load_run(self.runs_dir, run_id)
            offered = {(s["start"], s["end"]) for s in view.spans}
            mine = {(k[1], k[2]) for k in labels if k[0] == run_id}
            out.append({"id": run_id, "total": len(offered | mine), "labeled": len(mine)})
        return out

    def run(self, run_id: str) -> dict[str, Any]:
        view = load_run(self.runs_dir, run_id).to_json()
        view["labels"] = [
            {
                "start": r.start,
                "end": r.end,
                "text": r.text,
                "label": r.label,
                "source": r.source,
                "note": r.note,
            }
            for k, r in sorted(store.load(self.path).items())
            if k[0] == run_id
        ]
        return view

    def label(self, body: dict[str, Any]) -> dict[str, Any]:
        run_id, start, end = str(body["run"]), int(body["start"]), int(body["end"])
        answer = load_run(self.runs_dir, run_id).answer
        text = answer[start:end]
        # The span must still index the same text: labels outlive edits.
        if not text or text != body.get("text"):
            raise ValueError("span does not match the answer text")
        store.append(
            self.path,
            store.LabelRecord(
                run=run_id,
                start=start,
                end=end,
                text=text,
                label=str(body["label"]),
                labeler=self.labeler,
                at=datetime.now(UTC).isoformat(timespec="seconds"),
                source=str(body.get("source", "token")),
                note=str(body.get("note", "")),
            ),
        )
        return self.run(run_id)


def _handler(app: LabelApp) -> type[BaseHTTPRequestHandler]:
    page = resources.files("vouch_harness.label").joinpath("static/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _local_hosts(self) -> set[str]:
            assert isinstance(self.server, HTTPServer)
            port = self.server.server_port
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _forbidden(self) -> bool:
            """Binding to 127.0.0.1 does not stop a web page the labeler
            has open from reaching this server. A Host check defeats DNS
            rebinding (a hostile name resolving to 127.0.0.1 reads runs);
            an Origin check defeats cross-site form posts. Answers the
            request and returns True when it is refused."""
            if self.headers.get("Host") not in self._local_hosts():
                self._json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
                return True
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in self._local_hosts()}:
                self._json({"error": "cross-origin request refused"}, HTTPStatus.FORBIDDEN)
                return True
            return False

        def do_GET(self) -> None:
            if self._forbidden():
                return
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
                elif url.path == "/api/session":
                    self._json({"labeler": app.labeler, "labels": list(store.LABELS)})
                elif url.path == "/api/runs":
                    self._json(app.runs())
                elif url.path == "/api/run":
                    self._json(app.run(parse_qs(url.query)["id"][0]))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (KeyError, ValueError, FileNotFoundError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            if self._forbidden():
                return
            if urlparse(self.path).path != "/api/label":
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            # A form or text/plain POST is a CORS "simple request" that a
            # browser sends cross-site without asking; application/json
            # needs a preflight, which this server never grants.
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._json(
                    {"error": "Content-Type must be application/json"},
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self._json(app.label(body))
            except (KeyError, ValueError, TypeError, FileNotFoundError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vouch-label",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--runs", type=Path, default=Path("eval/runs"))
    p.add_argument("--labels", type=Path, default=Path("eval/labels"))
    sub = p.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="open the labeling UI")
    serve.add_argument("--labeler", required=True)
    serve.add_argument("--port", type=int, default=8765)
    agree = sub.add_parser("agreement", help="Cohen's kappa between two labelers")
    agree.add_argument("a")
    agree.add_argument("b")
    sub.add_parser("stats", help="label counts per labeler")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        app = LabelApp(args.runs, args.labels, args.labeler)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler(app))
        print(
            f"vouch-label: http://127.0.0.1:{server.server_port}/ as {app.labeler}; "
            f"{len(discover(args.runs))} runs; labels -> {app.path}",
            file=sys.stderr,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    if args.cmd == "agreement":
        a = store.load(args.labels / f"{args.a}.jsonl")
        b = store.load(args.labels / f"{args.b}.jsonl")
        r = agreement(a, b)
        print(f"shared spans: {r.shared} (only {args.a}: {r.only_a}, only {args.b}: {r.only_b})")
        print(f"observed agreement: {r.observed:.3f}   Cohen's kappa: {r.kappa:.3f}")
        for (la, lb), n in sorted(r.confusion.items(), key=lambda kv: -kv[1]):
            if la != lb:
                print(f"  {args.a}={la:<13} {args.b}={lb:<13} {n}")
        return 0
    for path in sorted(args.labels.glob("*.jsonl")):
        labels = store.load(path)
        counts = Counter(rec.label for rec in labels.values())
        print(
            f"{path.stem}: {len(labels)} spans; "
            + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
