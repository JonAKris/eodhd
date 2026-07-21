"""
core.contract
=============
The single contract every strategy conforms to.

Two turns of stress-testing against real code converged the interface here.
The programs being merged produce five different shapes -- a rich screener
study (SSG), a bare cross-sectional factor (momentum/value), a caveat-carrying
snapshot signal (institutional/fund flow), a clean event signal (insider
transactions), and non-signals that are pure presentation (market breadth,
movers). A bare `float` return could not carry all of them: an institutional
distribution reading taken while the holder list is capped is a *floor, not a
point estimate*, and the SQL layer refuses to launder that fact away -- so the
strategy layer must not either. The scalar and its caveats have to travel
together.

Hence `Signal`: one rankable scalar plus the structured context a consumer
needs to use it honestly. This collapses the `signal()` / `study()` split I
floated earlier into one `evaluate()` method with two views of its result --
the harness ranks on `.value`, the newsletter reads `.flags` and `.detail`,
and a screener puts its whole study in `.detail`. No `Screener` subtype needed.

The cardinal rule, inherited straight from the newsletter's design ethos:
`value is None` means "not known / not rankable at this as_of." A consumer must
treat it as *exclude*, never as zero. Zero is a real reading (flat flow); None
is the absence of one. Conflating them is exactly the kind of fabrication the
whole pipeline is built to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

# Forward reference only -- avoids a circular import with core.context.
# Context is passed in at call time; strategies never construct their own.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .context import Context


@dataclass
class Signal:
    """The result of evaluating one strategy for one ticker at one as_of.

    Attributes
    ----------
    value:
        The rankable scalar. ``None`` means "no rankable reading at this
        as_of" -- below a validity floor, not covered, or (crucially) not yet
        observed as of the requested date. Never substitute 0 for None.
    as_of:
        The date the caller asked about. Carried back so a consumer can never
        mistake which vintage this reading belongs to.
    flags:
        Structured caveats that must ride with the scalar. For an excluded
        signal this carries ``reason``. For a live one it carries whatever the
        consumer needs to use the number honestly -- e.g. ``top_n_at_cap`` and
        ``reading_is_lower_bound`` for flow signals, so the harness can segment
        capped from clean readings instead of being blind to the bias.
    detail:
        Free-form supporting data for narration or a full study. The newsletter
        reads its ``top_movers`` here; a screener drops its entire result here.
        Never required for ranking.
    """

    value: float | None
    as_of: date
    flags: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    @property
    def is_rankable(self) -> bool:
        """True iff this signal carries a scalar a ranker may use."""
        return self.value is not None

    @classmethod
    def excluded(cls, as_of: date, reason: str, **flags) -> "Signal":
        """A non-reading. `reason` explains why there is no scalar, so callers
        can log or omit sections without guessing."""
        f = {"reason": reason}
        f.update(flags)
        return cls(value=None, as_of=as_of, flags=f)


@runtime_checkable
class Strategy(Protocol):
    """The one interface. A strategy maps (ticker, as_of, context) to a Signal.

    Contract obligations every implementation owes its callers:

    1. NO LOOK-AHEAD. `evaluate(t, as_of)` may use only information that was
       observable on or before `as_of`. A strategy backed by a point-in-time
       snapshot must return ``Signal.excluded(..., 'no_vintage_as_of')`` when
       asked about a date earlier than anything it actually observed, rather
       than reconstructing a plausible past reading. This is the property that
       lets the same strategy serve both the live newsletter and the backtest
       harness without one corrupting the other.

    2. VALIDITY FLOORS TRAVEL WITH THE SIGNAL. A reading below a data-validity
       floor (denominator noise, too few filers) is not a small signal -- it is
       *no* signal. Return None, not a tiny number the harness would IC-test as
       if it were real.

    3. CAVEATS ARE NOT OPTIONAL. If the reading is biased or bounded, say so in
       `flags`. Do not hand a ranker a number that looks like a point estimate
       when the data says it is a floor.
    """

    name: str

    def evaluate(self, ticker: str, as_of: date, ctx: "Context") -> Signal: ...
