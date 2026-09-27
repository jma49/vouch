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
    """Whether a fact's data is from after the simulated moment. A fact
    with no date or an unreadable one is not judged a violation here;
    it is also never in any claim's time window."""
    if not fact.as_of:
        return False
    try:
        return parse_moment(fact.as_of) > as_of
    except ValueError:
        return False


@dataclass(frozen=True)
class LookAhead:
    """One receipt that carries data from after the simulated moment."""

    receipt_id: str
    tool_name: str
    latest: str  # the receipt's latest data timestamp, as written


def find_lookahead(receipts: list[Receipt], as_of: datetime) -> list[LookAhead]:
    out = []
    for r in receipts:
        stamps = [s for s in (r.data_asof, *(f.as_of for f in r.facts)) if s]
        future = []
        for s in stamps:
            try:
                if parse_moment(s) > as_of:
                    future.append(s)
            except ValueError:
                continue
        if future:
            out.append(LookAhead(r.receipt_id, r.tool_name, max(future, key=parse_moment)))
    return out
