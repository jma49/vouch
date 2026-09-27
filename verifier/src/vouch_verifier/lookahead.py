"""Look-ahead detection for backtests (design section 8.4).

A backtest runs an agent as of a simulated moment. Every receipt names
the time its data is from (data_asof, and as_of on each fact), so
whether the agent saw data from after that moment is a question about
the receipts, not about the agent's code: any later timestamp is a
look-ahead violation.

A date without a time counts as the end of that day, both for the
simulated moment and for the data. That is the strict reading: a daily
close dated July 24 is flagged under an as-of of July 24 at 15:00,
because the close is not known until the day ends. The verifier never
guesses toward a pass (AGENTS.md invariant 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time

from vouch_verifier.receipts import Fact, Receipt


def parse_moment(text: str) -> datetime:
    """An ISO 8601 date or date-time as an aware UTC datetime.

    A bare date is the last instant of that day; a naive date-time is
    taken as UTC. Raises ValueError for anything else.
    """
    text = text.strip()
    try:
        if len(text) == 10:
            day = datetime.strptime(text, "%Y-%m-%d").date()
            return datetime.combine(day, time.max, tzinfo=UTC)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"not an ISO 8601 date or time: {text!r}") from e
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def after(fact: Fact, as_of: datetime) -> bool:
    """Whether a fact's data may be from after the simulated moment. A
    date the verifier cannot read counts as after: it cannot show the
    data predates the moment, and passing it would be a guess (#96)."""
    if not fact.as_of:
        return False
    return _later(fact.as_of, as_of)


def _later(stamp: str, as_of: datetime) -> bool:
    try:
        return parse_moment(stamp) > as_of
    except ValueError:
        return True


@dataclass(frozen=True)
class LookAhead:
    """One receipt that carries data from after the simulated moment."""

    receipt_id: str
    tool_name: str
    latest: str  # the receipt's latest data timestamp, as written


def find_lookahead(receipts: list[Receipt], as_of: datetime) -> list[LookAhead]:
    """Every receipt whose data may be from after the moment. A fact is
    dated as the matcher dates it: its own as_of, else the receipt's
    data_asof, else when the call was made (#96), so the list and the
    verdicts never disagree. An unreadable date counts as later."""
    out = []
    for r in receipts:
        dates = [f.as_of or r.data_asof or r.wall_time for f in r.facts]
        stamps = [s for s in (r.data_asof, *dates) if s]
        future = [s for s in stamps if _later(s, as_of)]
        if future:
            out.append(LookAhead(r.receipt_id, r.tool_name, max(future, key=_sort_key)))
    return out


def _sort_key(stamp: str) -> str:
    try:
        return parse_moment(stamp).isoformat()
    except ValueError:
        return "~" + stamp  # unreadable dates sort last, as the most suspect
