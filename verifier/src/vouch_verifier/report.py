"""Verdict report rendering (design section 10).

Every report states the tier mix and the tolerance policy it was
computed under; a verdict without its policy is not reproducible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from vouch_verifier.claims import Extraction
from vouch_verifier.matcher import MatchedClaim
from vouch_verifier.verdict import Tolerance, Verdict


@dataclass(frozen=True)
class Report:
    matched: tuple[MatchedClaim, ...]
    tolerances: dict[str, Tolerance]

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
    extraction: Extraction,
    matched: list[MatchedClaim],
    tolerances: dict[str, Tolerance],
) -> Report:
    del extraction  # all spans, resolved or not, are present in matched
    return Report(matched=tuple(matched), tolerances=tolerances)


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
        },
        "tolerance_policy": {
            name: {"abs": t.abs, "rel": t.rel, "display_rel": t.display_rel}
            for name, t in sorted(report.tolerances.items())
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


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
            f"| {mc.verdict.value} | `{mc.claim.text.strip()}` | {entity} | {metric} "
            f"| {receipted} | {rid} | {mc.note} |"
        )
    lines.append("")
    lines.append("## Tolerance policy")
    lines.append("")
    for name, t in sorted(report.tolerances.items()):
        lines.append(f"- `{name}`: abs={t.abs} rel={t.rel} display_rel={t.display_rel}")
    lines.append("")
    return "\n".join(lines)
