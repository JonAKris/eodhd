"""
selftest
========
Exercises the contract and every concrete strategy end to end with NO database,
by injecting a fake fetch_all into Context -- the offline idiom
ssg_screener.py uses. Run:

    python -m agent.selftest

The two strategies prove the contract on opposite shapes:
  * institutional_flow is a snapshot signal -> a historical as_of returns None.
  * insider is an event signal          -> a historical as_of returns a value.
Same Signal / Strategy / Context, both directions.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date

from .core.contract import Strategy
from .core.context import Context, Floors
from .strategies.institutional_flow import InstitutionalFlowStrategy
from .strategies.insider import InsiderStrategy
from .strategies.ssg import SSGStrategy
from .strategies.momentum import MomentumStrategy
from .strategies.value import ValueStrategy


# ---------------------------------------------------------------------------
# institutional_flow (snapshot signal)
# ---------------------------------------------------------------------------
def _inst_row(**over):
    base = dict(
        ticker="TEST", latest_filing=date(2026, 3, 31), observed_at=date(2026, 4, 5),
        banked_at=date(2026, 4, 6),
        top_n_holders=20, top_n_at_cap=True, holders_at_latest=19, holders_lagging=1,
        net_change_shares=1_200_000, prior_shares_at_latest=10_000_000,
        net_change_pct=12.0, top_n_pct_of_shares_out=41.2, largest_holder_pct=8.3,
        n_added=12, n_trimmed=3, n_initiated=2, n_unchanged=3,
    )
    base.update(over)
    return base


def _inst_ctx(row_or_none, floors=None, has_vintage=False):
    rows = [] if row_or_none is None else [row_or_none]

    def fetch(sql, params=None):
        if "information_schema.tables" in sql:      # vintage-table existence
            return [{"x": 1}] if has_vintage else []
        if "inst_flow_vintage LIMIT 1" in sql:      # non-empty check
            return [{"x": 1}] if has_vintage else []
        return rows                                  # live/banked/any-vintage data
    return Context(fetch_all=fetch, floors=floors or Floors())


def test_institutional() -> bool:
    strat = InstitutionalFlowStrategy()
    asof = date(2026, 4, 10)
    ok = True
    print("institutional_flow (live -> prospective-only; vintage -> backtestable):")

    s = strat.evaluate("TEST", asof, _inst_ctx(_inst_row()))
    print(f"  [1] accumulation        -> value={s.value} at_cap={s.flags.get('top_n_at_cap')}")
    ok &= s.is_rankable and s.value == 12.0 and s.flags["reading_is_lower_bound"] is False

    s = strat.evaluate("NOPE", asof, _inst_ctx(None))
    print(f"  [2] not covered         -> {s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "not_covered"

    s = strat.evaluate("TEST", asof,
                       _inst_ctx(_inst_row(prior_shares_at_latest=23_000, net_change_pct=1342.0)))
    print(f"  [3] tiny prior base     -> {s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "below_min_prior_shares"

    s = strat.evaluate("TEST", date(2025, 12, 31), _inst_ctx(_inst_row()))
    print(f"  [4] historical as_of    -> value={s.value} reason={s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "no_vintage_as_of"

    s = strat.evaluate("TEST", asof,
                       _inst_ctx(_inst_row(net_change_pct=-8.5, n_added=2, n_trimmed=14)))
    print(f"  [5] capped distribution -> value={s.value} lower_bound={s.flags.get('reading_is_lower_bound')}")
    ok &= s.is_rankable and s.value == -8.5 and s.flags["reading_is_lower_bound"] is True

    # [6] PROMOTION: with banked vintages, a historical as_of returns a REAL
    #     value (banked_at <= as_of), where the live path returned None in [4].
    strat_v = InstitutionalFlowStrategy()
    s = strat_v.evaluate("TEST", date(2025, 12, 31),
                         _inst_ctx(_inst_row(banked_at=date(2025, 11, 30)), has_vintage=True))
    print(f"  [6] vintage historical  -> value={s.value} knowability={s.flags.get('knowability')}")
    ok &= s.is_rankable and s.value == 12.0 and s.flags["knowability"] == "banked_at"
    return ok


# ---------------------------------------------------------------------------
# insider (event signal)
# ---------------------------------------------------------------------------
def _agg(**over):
    base = dict(n_txns=5, n_buys=4, n_sells=1, n_insiders=3,
                buy_value=900_000, sell_value=200_000,
                earliest=date(2026, 3, 20), latest=date(2026, 4, 8))
    base.update(over)
    return base


def _ins_ctx(agg_row, has_report_date=False):
    """Route the two queries insider issues: schema introspection vs aggregate."""
    def fetch_all(sql, params=None):
        if "information_schema.columns" in sql:
            return [{"present": 1}] if has_report_date else []
        return [agg_row]
    return Context(fetch_all=fetch_all, floors=Floors())


def test_insider() -> bool:
    asof = date(2026, 4, 10)
    ok = True
    print("\ninsider (event -> retrospectively backtestable):")

    strat = InsiderStrategy()
    s = strat.evaluate("TEST", asof, _ins_ctx(_agg()))
    print(f"  [1] net buying          -> value={s.value} gate={s.flags.get('filing_gate')} dir={s.flags.get('direction')}")
    ok &= s.is_rankable and s.value == 700_000.0 and s.flags["filing_gate"] == "proxy:4d"

    strat = InsiderStrategy()
    s = strat.evaluate("TEST", asof, _ins_ctx(_agg(buy_value=100_000, sell_value=800_000)))
    print(f"  [2] net selling         -> value={s.value} sales_are_noisier={s.flags.get('sales_are_noisier')}")
    ok &= s.is_rankable and s.value == -700_000.0 and s.flags["sales_are_noisier"] is True

    strat = InsiderStrategy()
    s = strat.evaluate("TEST", asof, _ins_ctx(_agg(n_txns=1)))
    print(f"  [3] below min txns      -> {s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "below_min_txns"

    strat = InsiderStrategy()
    s = strat.evaluate("TEST", asof, _ins_ctx(_agg(n_txns=0)))
    print(f"  [4] no activity         -> {s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "no_activity"

    # [5] KEYSTONE mirror: a historical as_of returns a REAL value.
    strat = InsiderStrategy()
    s = strat.evaluate("TEST", date(2025, 6, 30), _ins_ctx(_agg()))
    print(f"  [5] historical as_of    -> value={s.value} (real, unlike snapshot signal)")
    ok &= s.is_rankable and s.value == 700_000.0

    # [6] report_date column present -> per-row exact/proxy gate
    strat = InsiderStrategy()
    s = strat.evaluate("TEST", asof, _ins_ctx(_agg(), has_report_date=True))
    print(f"  [6] report_date present -> gate={s.flags.get('filing_gate')}")
    ok &= s.is_rankable and s.flags["filing_gate"] == "report_date|proxy:4d"

    # [7] net flat: 0.0 is a real reading, rankable, distinct from None
    strat = InsiderStrategy()
    s = strat.evaluate("TEST", asof, _ins_ctx(_agg(buy_value=500_000, sell_value=500_000)))
    print(f"  [7] net flat            -> value={s.value} rankable={s.is_rankable} dir={s.flags.get('direction')}")
    ok &= s.is_rankable and s.value == 0.0 and s.flags["direction"] == "net_flat"
    return ok


# ---------------------------------------------------------------------------
# ssg (rich screener: scalar in .value, full study in .detail)
# ---------------------------------------------------------------------------
@dataclass
class _FakeSSG:
    """Stands in for ssg_screener.SSGResult so the wrap is testable without the
    real 700-line module present. Only the fields the wrap reads are modelled."""
    total_return: float | None
    quality_pass: bool = False
    is_buy: bool = False
    zone: str | None = None
    updown_ratio: float | None = None
    fc_eps_method: str | None = None
    fc_backtest_err: float | None = None
    reasons: list = field(default_factory=list)


def _ssg_ctx(header_row, today):
    rows = [] if header_row is None else [header_row]
    return Context(fetch_all=lambda sql, params=None: rows, floors=Floors(), today=today)


def test_ssg() -> bool:
    today = date(2026, 7, 19)
    header = {"ticker": "TEST", "name": "Test Co", "sector": "Tech"}
    ok = True
    print("\nssg (rich screener -> scalar in .value, study in .detail):")

    # [1] rankable: study rides in detail, projected return is the scalar
    built = {"called": False}

    def build_buy(row):
        built["called"] = True
        return _FakeSSG(total_return=0.185, quality_pass=True, is_buy=True,
                        zone="BUY", updown_ratio=4.2, fc_eps_method="cagr_5yr")

    s = SSGStrategy(build_ssg=build_buy).evaluate("TEST", today, _ssg_ctx(header, today))
    print(f"  [1] buy-zone name       -> value={s.value} zone={s.flags.get('zone')} "
          f"detail_has_study={'quality_pass' in s.detail}")
    ok &= s.is_rankable and abs(s.value - 0.185) < 1e-9 and s.flags["zone"] == "BUY" \
          and s.detail.get("quality_pass") is True

    # [2] computed but no projection -> value None, study still in detail
    def build_fail(row):
        return _FakeSSG(total_return=None, quality_pass=False,
                        reasons=["ROE below 15% (9.1%)"])

    s = SSGStrategy(build_ssg=build_fail).evaluate("TEST", today, _ssg_ctx(header, today))
    print(f"  [2] no projection       -> value={s.value} reason={s.flags.get('reason')} "
          f"reasons_in_detail={bool(s.detail.get('reasons'))}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "no_projection" and bool(s.detail.get("reasons"))

    # [3] prospective gate: historical as_of -> excluded, build never called
    built2 = {"called": False}

    def build_guard(row):
        built2["called"] = True
        return _FakeSSG(total_return=0.2)

    s = SSGStrategy(build_ssg=build_guard).evaluate("TEST", date(2025, 12, 31),
                                                    _ssg_ctx(header, today))
    print(f"  [3] historical as_of    -> reason={s.flags.get('reason')} "
          f"build_skipped={not built2['called']}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "no_pit_fundamentals" \
          and built2["called"] is False

    # [4] not covered: no header row
    s = SSGStrategy(build_ssg=build_buy).evaluate("NOPE", today, _ssg_ctx(None, today))
    print(f"  [4] not covered         -> reason={s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "not_covered"
    return ok


# ---------------------------------------------------------------------------
# momentum (price factor: point-in-time, backtestable)
# ---------------------------------------------------------------------------
def _price_ctx(series):
    """series: list of (iso_date_str, price). Fake returns it for eod_prices."""
    rows = [{"date": date.fromisoformat(d), "adjusted_close": p} for d, p in series]
    return Context(fetch_all=lambda sql, params=None: rows, floors=Floors())


def test_momentum() -> bool:
    ok = True
    strat = MomentumStrategy()
    print("\nmomentum (price factor -> backtestable at any as_of):")

    # [1] historical as_of, uptrend -> real value (proves backtestability)
    #     as_of 2025-12-31: recent_target ~2025-11-28, old_target ~2024-12-28
    s = strat.evaluate("TEST", date(2025, 12, 31),
                       _price_ctx([("2024-12-20", 100.0), ("2025-11-20", 120.0)]))
    print(f"  [1] historical uptrend  -> value={s.value} rankable={s.is_rankable}")
    ok &= s.is_rankable and abs(s.value - 0.20) < 1e-9

    # [2] downtrend -> negative
    s = strat.evaluate("TEST", date(2025, 12, 31),
                       _price_ctx([("2024-12-20", 120.0), ("2025-11-20", 90.0)]))
    print(f"  [2] downtrend           -> value={s.value}")
    ok &= s.is_rankable and s.value < 0

    # [3] insufficient history -> excluded
    s = strat.evaluate("TEST", date(2025, 12, 31), _price_ctx([("2025-11-20", 120.0)]))
    print(f"  [3] one price only      -> {s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "insufficient_price_history"

    # [4] stale window -> excluded (recent leg far from its target)
    s = strat.evaluate("TEST", date(2026, 6, 30),
                       _price_ctx([("2025-06-25", 100.0), ("2026-04-01", 130.0)]))
    print(f"  [4] stale price window  -> {s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "stale_price_window"
    return ok


# ---------------------------------------------------------------------------
# value (earnings yield: point-in-time, backtestable)
# ---------------------------------------------------------------------------
def _val_ctx(eps_rows, price):
    def fetch(sql, params=None):
        if "earnings_history" in sql:
            return eps_rows
        if "eod_prices" in sql:
            return [{"adjusted_close": price}] if price is not None else []
        return []
    return Context(fetch_all=fetch, floors=Floors())


def _eps(*vals):
    # most-recent-first, as the SQL returns
    return [{"date": date(2025, 12, 31), "eps_actual": v} for v in vals]


def test_value() -> bool:
    ok = True
    strat = ValueStrategy()
    print("\nvalue (earnings yield -> backtestable at any as_of):")

    # [1] historical as_of, positive yield
    s = strat.evaluate("TEST", date(2026, 1, 15), _val_ctx(_eps(1.2, 1.1, 1.0, 0.9), 84.0))
    print(f"  [1] positive yield      -> value={round(s.value,4)} rankable={s.is_rankable}")
    ok &= s.is_rankable and abs(s.value - (4.2 / 84.0)) < 1e-9

    # [2] loss-maker -> negative yield, flagged not dropped
    s = strat.evaluate("TEST", date(2026, 1, 15), _val_ctx(_eps(-0.5, -0.3, 0.1, 0.1), 20.0))
    print(f"  [2] negative earnings   -> value={round(s.value,4)} flag={s.flags.get('negative_earnings')}")
    ok &= s.is_rankable and s.value < 0 and s.flags["negative_earnings"] is True

    # [3] insufficient quarters -> excluded
    s = strat.evaluate("TEST", date(2026, 1, 15), _val_ctx(_eps(1.0, 0.9), 50.0))
    print(f"  [3] two quarters only   -> {s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "insufficient_eps_history"

    # [4] no price -> excluded
    s = strat.evaluate("TEST", date(2026, 1, 15), _val_ctx(_eps(1.0, 1.0, 1.0, 1.0), None))
    print(f"  [4] no price            -> {s.flags.get('reason')}")
    ok &= (not s.is_rankable) and s.flags["reason"] == "no_price"
    return ok


# ---------------------------------------------------------------------------
# select mode (registry-free runner: sort direction + exclusion filtering)
# ---------------------------------------------------------------------------
def test_select() -> bool:
    from .modes.select import run_select
    from .core.contract import Signal as _Sig

    class _Stub:
        name = "stub"

        def __init__(self, m):
            self.m = m

        def evaluate(self, ticker, as_of, ctx):
            v = self.m.get(ticker)
            return _Sig(v, as_of, {}) if v is not None else _Sig.excluded(as_of, "none")

    ok = True
    print("\nselect mode (runner: ranks rankable, drops excluded):")
    ctx = Context(fetch_all=lambda sql, params=None: [], floors=Floors())
    as_of = date(2026, 7, 21)
    scores = {"A": 3.0, "B": 1.0, "C": 2.0, "D": None}  # D is non-rankable
    strat = _Stub(scores)

    ranked, stats = run_select(ctx, strat, ["A", "B", "C", "D"], as_of)
    order = [r.ticker for r in ranked]
    print(f"  [1] descending          -> {order}  rankable={stats['rankable']} excluded={stats['excluded']}")
    ok &= order == ["A", "C", "B"] and stats["rankable"] == 3 and stats["excluded"] == 1

    ranked2, _ = run_select(ctx, strat, ["A", "B", "C", "D"], as_of, ascending=True)
    print(f"  [2] ascending           -> {[r.ticker for r in ranked2]}")
    ok &= [r.ticker for r in ranked2] == ["B", "C", "A"]

    # ranks are 1-based and contiguous
    ok &= [r.rank for r in ranked] == [1, 2, 3]
    return ok


def main() -> int:
    ok = True
    ok &= test_institutional()
    ok &= test_insider()
    ok &= test_ssg()
    ok &= test_momentum()
    ok &= test_value()
    ok &= test_select()

    conforms = all(isinstance(s, Strategy) for s in
                   (InstitutionalFlowStrategy(), InsiderStrategy(), SSGStrategy(),
                    MomentumStrategy(), ValueStrategy()))
    print(f"\nall five conform to Strategy protocol -> {conforms}")
    ok &= conforms

    print("\nSELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
