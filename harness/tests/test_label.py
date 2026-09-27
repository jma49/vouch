"""Labeling tool: label store, run loading, validation, agreement, HTTP."""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from vouch_harness.label import server as label_server
from vouch_harness.label import store
from vouch_harness.label.agreement import agreement, cohen_kappa
from vouch_harness.label.runs import discover, load_run
from vouch_harness.label.server import LabelApp, _handler

ANSWER = "NVDA is trading at 160.36, up 1.15% on July 24."


def make_run(runs: Path, run_id: str = "m/t01/s0", answer: str = ANSWER) -> Path:
    d = runs / run_id
    d.mkdir(parents=True)
    (d / "answer.txt").write_text(answer + "\n")
    (d / "meta.json").write_text(json.dumps({"prompt": "How is NVDA?", "finished": True}))
    transcript = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "function": {"name": "get_quote", "arguments": '{"symbol":"NVDA"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"last": 160.36}'},
    ]
    (d / "transcript.json").write_text(json.dumps(transcript))
    return d


def record(label: str, start: int = 19, end: int = 25, run: str = "m/t01/s0") -> store.LabelRecord:
    return store.LabelRecord(
        run=run,
        start=start,
        end=end,
        text=ANSWER[start:end],
        label=label,
        labeler="a",
        at="2026-09-27T00:00:00+00:00",
    )


def test_store_latest_record_wins_and_cleared_drops(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    store.append(path, record("CONTRADICTED"))
    store.append(path, record("SUPPORTED"))
    store.append(path, record("UNSUPPORTED", start=0, end=4))
    store.append(path, record("CLEARED", start=0, end=4))
    current = store.load(path)
    assert [r.label for r in current.values()] == ["SUPPORTED"]
    assert len(path.read_text().splitlines()) == 4  # history is kept


def test_store_rejects_unknown_labels_and_bad_spans(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown label"):
        store.append(tmp_path / "a.jsonl", record("MAYBE"))
    with pytest.raises(ValueError, match="bad span"):
        store.append(tmp_path / "a.jsonl", record("SUPPORTED", start=5, end=5))


def test_load_run_offers_tokenizer_spans_and_tool_evidence(tmp_path: Path) -> None:
    make_run(tmp_path)
    assert discover(tmp_path) == ["m/t01/s0"]
    view = load_run(tmp_path, "m/t01/s0")
    assert [s["text"] for s in view.spans] == ["160.36", "1.15%"]  # the date is not offered
    assert view.tool_calls == [
        {"name": "get_quote", "arguments": '{"symbol":"NVDA"}', "result": '{"last": 160.36}'}
    ]


@pytest.mark.parametrize("bad", ["../x/y/z", "m/t01", "m/t01/s0/../../..", "/etc/passwd"])
def test_load_run_rejects_paths_outside_the_runs_dir(tmp_path: Path, bad: str) -> None:
    make_run(tmp_path)
    with pytest.raises((ValueError, FileNotFoundError)):
        load_run(tmp_path, bad)


def test_app_validates_span_text_before_writing(tmp_path: Path) -> None:
    make_run(tmp_path / "runs")
    app = LabelApp(tmp_path / "runs", tmp_path / "labels", "alice")
    view = app.label(
        {"run": "m/t01/s0", "start": 19, "end": 25, "text": "160.36", "label": "SUPPORTED"}
    )
    assert view["labels"][0]["label"] == "SUPPORTED"
    with pytest.raises(ValueError, match="does not match"):
        app.label(
            {"run": "m/t01/s0", "start": 19, "end": 25, "text": "999.99", "label": "SUPPORTED"}
        )
    assert app.runs() == [{"id": "m/t01/s0", "total": 2, "labeled": 1}]


def test_labeler_names_are_restricted(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LabelApp(tmp_path, tmp_path, "../evil")


def test_kappa() -> None:
    assert cohen_kappa([("A", "A"), ("B", "B")]) == 1.0
    # Classic example: 20 items, observed 0.7, expected 0.5 -> kappa 0.4.
    pairs = [("Y", "Y")] * 7 + [("Y", "N")] * 3 + [("N", "Y")] * 3 + [("N", "N")] * 7
    assert cohen_kappa(pairs) == pytest.approx(0.4)


def test_agreement_over_shared_spans() -> None:
    a = {r.key: r for r in [record("SUPPORTED"), record("UNSUPPORTED", 0, 4)]}
    b = {r.key: r for r in [record("CONTRADICTED"), record("SUPPORTED", 27, 32)]}
    result = agreement(a, b)
    assert (result.shared, result.only_a, result.only_b) == (1, 1, 1)
    assert result.confusion == {("SUPPORTED", "CONTRADICTED"): 1}


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    make_run(tmp_path / "runs")
    app = LabelApp(tmp_path / "runs", tmp_path / "labels", "alice")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(app))
    thread = threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> Any:
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def test_http_api_round_trip(server: str) -> None:
    assert b"vouch label" in _get(server + "/")
    assert json.loads(_get(server + "/api/session"))["labeler"] == "alice"
    run = json.loads(_get(server + "/api/run?id=m/t01/s0"))
    assert "verdict" not in json.dumps(run).lower()  # blind: no verifier output
    start = ANSWER.index("1.15%")
    body = json.dumps(
        {"run": "m/t01/s0", "start": start, "end": start + 5, "text": "1.15%", "label": "SUPPORTED"}
    ).encode()
    req = urllib.request.Request(
        server + "/api/label",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        assert json.loads(resp.read())["labels"][0]["text"] == "1.15%"


def _raw(
    server: str, method: str, path: str, headers: dict[str, str], body: bytes | None = None
) -> tuple[int, bytes]:
    """A request with exactly these headers (urllib would fill in Host)."""
    port = int(server.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _label_body() -> bytes:
    start = ANSWER.index("1.15%")
    return json.dumps(
        {"run": "m/t01/s0", "start": start, "end": start + 5, "text": "1.15%", "label": "SUPPORTED"}
    ).encode()


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Type": "text/plain"}, 415),  # a CORS simple request: no preflight
        ({"Content-Type": "application/json", "Origin": "https://evil.example"}, 403),
        ({"Content-Type": "application/json", "Origin": "null"}, 403),
        ({"Content-Type": "application/json", "Host": "attacker.example"}, 403),
    ],
)
def test_http_api_rejects_forged_label_writes(
    server: str, tmp_path: Path, headers: dict[str, str], status: int
) -> None:
    port = server.rsplit(":", 1)[1]
    sent = {"Host": f"127.0.0.1:{port}", **headers}
    assert _raw(server, "POST", "/api/label", sent, _label_body())[0] == status
    assert not (tmp_path / "labels" / "alice.jsonl").exists()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_http_api_accepts_same_origin_writes(server: str, host: str) -> None:
    port = server.rsplit(":", 1)[1]
    headers = {
        "Host": f"{host}:{port}",
        "Origin": f"http://{host}:{port}",
        "Content-Type": "application/json; charset=utf-8",
    }
    status, body = _raw(server, "POST", "/api/label", headers, _label_body())
    assert status == 200, body
    assert json.loads(body)["labels"][0]["label"] == "SUPPORTED"


@pytest.mark.parametrize(
    ("host", "status"),
    [("attacker.example", 403), ("127.0.0.1:1", 403), (None, 403), ("localhost:{port}", 200)],
)
def test_http_api_checks_the_host_header(server: str, host: str | None, status: int) -> None:
    port = server.rsplit(":", 1)[1]
    headers = {} if host is None else {"Host": host.format(port=port)}
    assert _raw(server, "GET", "/api/runs", headers)[0] == status


def test_http_api_rejects_bad_requests(server: str) -> None:
    for path in ("/api/run?id=../../x", "/api/run", "/nope"):
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(server + path)
        assert err.value.code in (400, 404)
        err.value.close()


# Outside the BMP: one code point in Python, two UTF-16 units in the page.
EMOJI_ANSWER = "\U0001f4c8 NVDA closed at 181.52, and volume was about 190 million."


def test_server_spans_are_code_point_offsets(tmp_path: Path) -> None:
    make_run(tmp_path / "runs", answer=EMOJI_ANSWER)
    view = load_run(tmp_path / "runs", "m/t01/s0")
    spans = [(s["start"], s["end"], s["text"]) for s in view.spans]
    assert spans == [(17, 23, "181.52"), (46, 57, "190 million")]
    app = LabelApp(tmp_path / "runs", tmp_path / "labels", "alice")
    start = EMOJI_ANSWER.index("about 190 million")
    body = {"run": "m/t01/s0", "start": start, "end": start + 17, "text": "about 190 million"}
    app.label({**body, "label": "SUPPORTED", "source": "manual"})


NODE = shutil.which("node")


def page_script(block: str) -> str:
    """One marked block of the page's script, for testing it in node."""
    html = Path(label_server.__file__).with_name("static").joinpath("index.html").read_text()
    begin, end = f"// BEGIN {block}\n", f"// END {block}\n"
    return html[html.index(begin) + len(begin) : html.index(end)]


def run_node(tmp_path: Path, script: str) -> None:
    assert NODE is not None
    path = tmp_path / "test.js"
    path.write_text('"use strict";\n' + script)
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_page_script_parses(tmp_path: Path) -> None:
    html = Path(label_server.__file__).with_name("static").joinpath("index.html").read_text()
    script = html[html.index("<script>") + len("<script>") : html.index("</script>")]
    path = tmp_path / "page.js"
    path.write_text(script)
    assert NODE is not None
    proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_page_converts_offsets_between_code_points_and_utf16(tmp_path: Path) -> None:
    make_run(tmp_path / "runs", answer=EMOJI_ANSWER)
    spans = load_run(tmp_path / "runs", "m/t01/s0").spans
    selected = "about 190 million"
    start_cp = EMOJI_ANSWER.index(selected)
    checks = f"""
const assert = require("node:assert/strict");
const answer = {json.dumps(EMOJI_ANSWER)};
const off = offsetMap(answer);
// Drawing: a server span, mapped to UTF-16, slices exactly its text.
for (const s of {json.dumps(spans)}) {{
  assert.equal(answer.slice(off.toU16(s.start), off.toU16(s.end)), s.text);
}}
// Selecting: DOM (UTF-16) offsets map back to the server's code points.
const u16 = answer.indexOf({json.dumps(selected)});
assert.equal(off.toCp(u16), {start_cp});
assert.equal(off.toCp(u16 + {len(selected)}), {start_cp + len(selected)});
assert.equal(off.toCp(0), 0);
assert.equal(off.toU16(0), 0);
assert.equal(off.toCp(answer.length), {len(EMOJI_ANSWER)});
assert.equal(off.toU16({len(EMOJI_ANSWER)}), answer.length);
"""
    run_node(tmp_path, page_script("offsets") + checks)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_page_keeps_unsaved_manual_spans_across_refreshes(tmp_path: Path) -> None:
    checks = """
const assert = require("node:assert/strict");
const token = (start, end, text) => ({ start, end, text });
const label = (start, end, text, lbl, source) =>
  ({ start, end, text, label: lbl, source, note: "" });
const spans = [token(19, 25, "160.36"), token(30, 35, "1.15%")];
const unsaved = new Map();
const keys = (list) => list.map((s) => `${s.start}:${s.end}:${s.label || "-"}:${s.source}`);
// Two missed numbers added by hand, not yet labeled.
unsaved.set("r", [
  { start: 0, end: 4, text: "NVDA", source: "manual" },
  { start: 40, end: 42, text: "24", source: "manual" },
]);
// Labeling a different span: the server reply carries only labeled spans.
let run = { id: "r", spans, labels: [label(19, 25, "160.36", "SUPPORTED", "token")] };
assert.deepEqual(keys(mergeSpans(run, unsaved)),
  ["0:4:-:manual", "19:25:SUPPORTED:token", "30:35:-:token", "40:42:-:manual"]);
// Labeling a manual span saves it; it is no longer pending.
run.labels.push(label(0, 4, "NVDA", "UNSUPPORTED", "manual"));
assert.deepEqual(keys(mergeSpans(run, unsaved)),
  ["0:4:UNSUPPORTED:manual", "19:25:SUPPORTED:token", "30:35:-:token", "40:42:-:manual"]);
assert.deepEqual(unsaved.get("r").map((s) => s.start), [40]);
// Clearing that label removes the span instead of resurrecting it.
run.labels.pop();
assert.deepEqual(keys(mergeSpans(run, unsaved)),
  ["19:25:SUPPORTED:token", "30:35:-:token", "40:42:-:manual"]);
// Another run's unsaved spans stay out.
assert.equal(mergeSpans({ id: "other", spans: [], labels: [] }, unsaved).length, 0);
"""
    run_node(tmp_path, page_script("spans") + checks)
