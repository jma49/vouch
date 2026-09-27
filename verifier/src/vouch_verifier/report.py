"""Verdict report rendering (design section 10).

Every report states the tier mix and the tolerance policy it was
computed under; a verdict without its policy is not reproducible.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

from vouch_verifier.lookahead import LookAhead
from vouch_verifier.matcher import MatchedClaim
from vouch_verifier.verdict import Tolerance, Verdict


@dataclass(frozen=True)
class Report:
    matched: tuple[MatchedClaim, ...]
    tolerances: dict[str, Tolerance]
    # Set when verifying a backtest (--as-of): the simulated moment and
    # every receipt carrying data from after it (design section 8.4).
    as_of: str | None = None
    lookahead: tuple[LookAhead, ...] = ()
    answer: str = ""  # the text claim spans index into, for the HTML report
    # False when the log was read without a public key: every report
    # format says so, since a published report otherwise looks verified
    # (#96).
    signatures_verified: bool = True

    @property
    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {v.value: 0 for v in Verdict}
        for mc in self.matched:
            c[mc.verdict.value] += 1
        return c

    @property
    def coverage(self) -> float:
        """Fraction of numeric spans with a verdict other than UNVERIFIABLE."""
        if not self.matched:
            return 0.0
        judged = sum(1 for mc in self.matched if mc.verdict is not Verdict.UNVERIFIABLE)
        return judged / len(self.matched)

    def tier_share(self, tier: int) -> float:
        if not self.matched:
            return 0.0
        return sum(1 for mc in self.matched if mc.claim.tier == tier) / len(self.matched)


def build_report(
    matched: list[MatchedClaim],
    tolerances: dict[str, Tolerance],
    as_of: str | None = None,
    lookahead: list[LookAhead] | None = None,
    answer: str = "",
    signatures_verified: bool = True,
) -> Report:
    return Report(
        matched=tuple(matched),
        tolerances=tolerances,
        as_of=as_of,
        lookahead=tuple(lookahead or ()),
        answer=answer,
        signatures_verified=signatures_verified,
    )


def to_json(report: Report) -> str:
    payload = {
        "verdicts": [
            {
                "verdict": mc.verdict.value,
                "value": mc.claim.value,
                "span": list(mc.claim.span),
                "text": mc.claim.text,
                "tier": mc.claim.tier,
                "entity": mc.claim.entity or (mc.fact.entity if mc.fact else None),
                "metric": mc.claim.metric or (mc.fact.metric if mc.fact else None),
                "receipt_id": mc.receipt_id,
                "fact_value": mc.fact.value if mc.fact else None,
                "json_ptr": mc.fact.json_ptr if mc.fact else None,
                "note": mc.note or None,
            }
            for mc in report.matched
        ],
        "summary": {
            "counts": report.counts,
            "coverage": report.coverage,
            "tier1_share": report.tier_share(1),
            "tier2_share": report.tier_share(2),
            "signatures_verified": report.signatures_verified,
        },
        "tolerance_policy": {name: t.as_dict() for name, t in sorted(report.tolerances.items())},
    }
    if report.as_of is not None:
        payload["lookahead"] = {
            "as_of": report.as_of,
            "receipts": [
                {"receipt_id": la.receipt_id, "tool": la.tool_name, "latest_data": la.latest}
                for la in report.lookahead
            ],
        }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def md_cell(value: object) -> str:
    """Make any value safe inside a markdown table cell.

    Claim text, json pointers, notes, and upstream-supplied entity and
    metric names all reach report tables. Unescaped, a "|" adds cells
    and raw HTML passes through to renderers that allow it (issue #18).
    """
    text = html.escape(str(value), quote=False)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
        # Link and image syntax: an upstream-supplied name must not
        # render as a link or fetch an image in a viewer (#96).
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("!", "\\!")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def to_markdown(report: Report) -> str:
    lines = ["# vouch verdict report", ""]
    counts = report.counts
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Claims judged: {len(report.matched)}")
    for verdict in Verdict:
        if counts[verdict.value]:
            lines.append(f"- {verdict.value}: {counts[verdict.value]}")
    lines.append(f"- Coverage (non-UNVERIFIABLE): {report.coverage:.0%}")
    lines.append(f"- Tier 1 (cited) share: {report.tier_share(1):.0%}")
    if not report.signatures_verified:
        lines.append("- **Signatures: not checked** (no public key was given)")
    lines.append("")
    if report.as_of is not None:
        lines.append("## Look-ahead")
        lines.append("")
        if not report.lookahead:
            lines.append(f"No receipt carries data from after {md_cell(report.as_of)}.")
        else:
            lines.append(
                f"{len(report.lookahead)} receipt(s) carry data from after "
                f"{md_cell(report.as_of)}: the agent saw the future."
            )
            lines.append("")
            lines.append("| receipt | tool | latest data |")
            lines.append("|---|---|---|")
            for la in report.lookahead:
                lines.append(
                    f"| {md_cell(la.receipt_id[:8])} | {md_cell(la.tool_name)} "
                    f"| {md_cell(la.latest)} |"
                )
        lines.append("")
    lines.append("## Claims")
    lines.append("")
    lines.append("| verdict | claim | entity | metric | receipted | receipt | note |")
    lines.append("|---|---|---|---|---|---|---|")
    for mc in report.matched:
        entity = mc.claim.entity or (mc.fact.entity if mc.fact else "")
        metric = mc.claim.metric or (mc.fact.metric if mc.fact else "")
        receipted = "" if mc.fact is None else str(mc.fact.value)
        rid = (mc.receipt_id or "")[:8]
        lines.append(
            f"| {mc.verdict.value} | `{md_cell(mc.claim.text.strip())}` | {md_cell(entity)} "
            f"| {md_cell(metric)} | {receipted} | {md_cell(rid)} | {md_cell(mc.note)} |"
        )
    lines.append("")
    lines.append("## Tolerance policy")
    lines.append("")
    for name, t in sorted(report.tolerances.items()):
        lines.append(f"- `{name}`: {t.describe()}")
    lines.append("")
    return "\n".join(lines)


# One letter per verdict beside each highlighted span, so the page does
# not rely on color alone.
_TAGS = {
    Verdict.SUPPORTED: "S",
    Verdict.CONTRADICTED: "C",
    Verdict.UNSUPPORTED: "U",
    Verdict.STALE: "T",
    Verdict.DERIVED: "D",
    Verdict.UNVERIFIABLE: "?",
}

# Verdict -> color token; light and dark values are the tokens below.
_TONES = {
    Verdict.SUPPORTED: "ok",
    Verdict.CONTRADICTED: "bad",
    Verdict.UNSUPPORTED: "miss",
    Verdict.STALE: "stale",
    Verdict.DERIVED: "derived",
    Verdict.UNVERIFIABLE: "na",
}
_LIGHT = {"ok": ("#1f7a3d", "#e3f4e8"), "bad": ("#b3261e", "#fbe4e2"),
          "miss": ("#9a5b00", "#fdf0d8"), "stale": ("#6a3fa0", "#efe6fa"),
          "derived": ("#1d5fa8", "#e2edfa"), "na": ("#6b6b70", "#efefed")}  # fmt: skip
_DARK = {"ok": ("#6fd08c", "#1c3324"), "bad": ("#ff8a80", "#3d1f1d"),
         "miss": ("#f0b95c", "#3a2d15"), "stale": ("#c4a4f0", "#2e2440"),
         "derived": ("#8ab8f0", "#1c2b40"), "na": ("#9d9da3", "#2a2a2e")}  # fmt: skip


def _tokens(base: dict[str, str], tones: dict[str, tuple[str, str]]) -> str:
    pairs = [*base.items()]
    for tone, (fg, bg) in tones.items():
        pairs += [(tone, fg), (f"{tone}-bg", bg)]
    return "\n".join(f"  --{k}: {v};" for k, v in pairs)


_CSS = "\n".join(
    [
        ":root {",
        _tokens(
            {
                "bg": "#fbfbfa",
                "fg": "#1d1d1f",
                "muted": "#6b6b70",
                "line": "#e2e2e0",
                "card": "#ffffff",
            },
            _LIGHT,
        ),
        "}",
        "@media (prefers-color-scheme: dark) { :root {",
        _tokens(
            {
                "bg": "#161618",
                "fg": "#ececef",
                "muted": "#9d9da3",
                "line": "#2e2e32",
                "card": "#1e1e21",
            },
            _DARK,
        ),
        "} }",
        "* { box-sizing: border-box; }",
        "body { margin: 0; background: var(--bg); color: var(--fg);",
        '  font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }',
        "main { max-width: 960px; margin: 0 auto; padding: 32px 16px 64px; }",
        "h1 { font-size: 22px; margin: 0 0 4px; }",
        "h2 { font-size: 16px; margin: 32px 0 12px; }",
        ".sub, .legend { color: var(--muted); }",
        ".legend { font-size: 13px; margin-top: 8px; }",
        ".chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; }",
        ".chip { list-style: none; border: 1px solid var(--line); border-radius: 999px;",
        "  padding: 2px 10px; background: var(--card); }",
        ".answer { background: var(--card); border: 1px solid var(--line); border-radius: 8px;",
        "  padding: 16px; white-space: pre-wrap; overflow-wrap: anywhere; }",
        "mark { color: inherit; border-radius: 3px; padding: 0 2px; border-bottom: 2px solid; }",
        "mark a { color: inherit; text-decoration: none; }",
        "sup.tag { font-size: 10px; font-weight: 600; margin-left: 1px; }",
        ".table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px;",
        "  background: var(--card); }",
        "table { border-collapse: collapse; width: 100%; font-size: 14px; }",
        "th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);",
        "  vertical-align: top; }",
        "th { color: var(--muted); font-weight: 600; }",
        "tr:last-child td { border-bottom: none; }",
        "tr:target { outline: 2px solid var(--fg); outline-offset: -2px; }",
        "code { font: 13px ui-monospace, SFMono-Regular, Menlo, monospace; }",
        ".verdict { font-weight: 600; white-space: nowrap; }",
        *(
            f".v-{v.value} {{ background: var(--{tone}-bg); border-color: var(--{tone}); }}\n"
            f"sup.v-{v.value} {{ color: var(--{tone}); background: none; }}"
            for v, tone in _TONES.items()
        ),
        ".v-UNVERIFIABLE { border-bottom-style: dotted; }",
    ]
)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _hint(mc: MatchedClaim) -> str:
    parts = [mc.verdict.value]
    if mc.fact is not None:
        parts.append(f"receipted {mc.fact.value:g}")
    if mc.receipt_id:
        parts.append(f"receipt {mc.receipt_id[:12]}")
    if mc.note:
        parts.append(mc.note)
    return " \u00b7 ".join(parts)


def _highlighted(answer: str, matched: tuple[MatchedClaim, ...]) -> str:
    """The answer, escaped, with each claim span wrapped in a mark that
    links to its table row. Overlapping spans keep the first."""
    out: list[str] = []
    pos = 0
    for i, mc in sorted(enumerate(matched), key=lambda im: im[1].claim.span):
        start, end = mc.claim.span
        if start < pos or end > len(answer):
            continue
        out.append(_esc(answer[pos:start]))
        v = mc.verdict.value
        out.append(
            f'<mark class="v-{v}" title="{_esc(_hint(mc))}"><a href="#c{i}">'
            f"{_esc(answer[start:end])}</a></mark>"
            f'<sup class="tag v-{v}" aria-label="{v}">{_TAGS[mc.verdict]}</sup>'
        )
        pos = end
    out.append(_esc(answer[pos:]))
    return "".join(out)


_UNSIGNED_HTML = " <strong>Signatures not checked</strong>: no public key was given."


def to_html(report: Report) -> str:
    """A self-contained page: the answer with every numeric span marked
    by verdict, then the details. The answer and everything from the
    receipts are untrusted, so all of it is escaped; the page has no
    scripts and loads nothing."""
    counts = report.counts
    chips = "".join(
        f'<li class="chip"><span class="verdict">{_esc(v.value)}</span> {counts[v.value]}</li>'
        for v in Verdict
        if counts[v.value]
    )
    rows = []
    for i, mc in enumerate(report.matched):
        entity = mc.claim.entity or (mc.fact.entity if mc.fact else "")
        metric = mc.claim.metric or (mc.fact.metric if mc.fact else "")
        receipted = "" if mc.fact is None else f"{mc.fact.value:g}"
        v = mc.verdict.value
        rows.append(
            f'<tr id="c{i}"><td><mark class="v-{v} verdict">{_esc(v)}</mark></td>'
            f"<td><code>{_esc(mc.claim.text.strip())}</code></td><td>{_esc(entity)}</td>"
            f"<td>{_esc(metric)}</td><td>{_esc(receipted)}</td>"
            f"<td><code>{_esc((mc.receipt_id or '')[:12])}</code></td><td>{_esc(mc.note)}</td></tr>"
        )
    lookahead = ""
    if report.as_of is not None:
        if report.lookahead:
            items = "".join(
                f"<tr><td><code>{_esc(la.receipt_id[:12])}</code></td><td>{_esc(la.tool_name)}</td>"
                f"<td>{_esc(la.latest)}</td></tr>"
                for la in report.lookahead
            )
            lookahead = (
                f"<h2>Look-ahead</h2><p>{len(report.lookahead)} receipt(s) carry data from after "
                f"{_esc(report.as_of)}: the agent saw the future.</p>"
                "<div class='table-wrap'><table>"
                f"<tr><th>Receipt</th><th>Tool</th><th>Latest data</th></tr>{items}</table></div>"
            )
        else:
            lookahead = (
                f"<h2>Look-ahead</h2><p>No receipt carries data from after "
                f"{_esc(report.as_of)}.</p>"
            )
    policy = "".join(
        f"<li><code>{_esc(name)}</code>: {_esc(t.describe())}</li>"
        for name, t in sorted(report.tolerances.items())
    )
    legend = " \u00b7 ".join(f"{tag} {v.value}" for v, tag in _TAGS.items())
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>vouch verdict report</title><style>{_CSS}</style></head><body><main>"
        "<h1>vouch verdict report</h1>"
        f'<p class="sub">{len(report.matched)} numeric claims; coverage {report.coverage:.0%}; '
        f"Tier 1 (cited) {report.tier_share(1):.0%}."
        f"{'' if report.signatures_verified else _UNSIGNED_HTML}</p>"
        f'<ul class="chips">{chips}</ul>'
        f'<h2>Answer</h2><div class="answer">{_highlighted(report.answer, report.matched)}</div>'
        f'<p class="legend">{_esc(legend)}. Hover or focus a number for details; select it for '
        "its row below.</p>"
        f"{lookahead}"
        '<h2>Claims</h2><div class="table-wrap"><table><tr><th>Verdict</th><th>Claim</th>'
        "<th>Entity</th><th>Metric</th><th>Receipted</th><th>Receipt</th><th>Note</th></tr>"
        f"{''.join(rows)}</table></div>"
        f"<h2>Tolerance policy</h2><ul>{policy}</ul>"
        "</main></body></html>\n"
    )
