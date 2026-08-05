#!/usr/bin/env python3
"""
newsletter.py -- Build and email the daily market newsletter.

This is the renderer for the grounded-facts newsletter layer. All the numbers
are computed in SQL by ``build_newsletter()`` (sql/02_newsletter_findings.sql)
and exposed as one JSON document by ``newsletter_payload()``. This module reads
that document and lays it out, in a fixed reader-facing order:

  1. Opening    -- market holidays (if any) and today's earnings reports
  2. Major markets -- the benchmark indices (DOW, S&P, Nasdaq, ...)
  3. Most active movers -- top advancers / decliners in the liquid universe
  4. Sector heat map -- cap-weighted 1-day return by sector
  5. Strategy picks -- explorer signals, bucketed Buy / Hold / Sell
  6. Stock Selection Guide -- SSG names, bucketed Buy / Hold / Sell
  7. News recap -- the day's financial headlines

The model, if narration is enabled, only rewrites prose from the supplied
facts. It never computes a number: every figure in the email comes from SQL.

Reuses the Outlook-safe HTML shell and the SMTP/DKIM sending machinery from
``morning_report.py`` so deliverability behaviour stays identical.

Usage:
  python -m explorer.newsletter                    # build, render, email
  python -m explorer.newsletter --no-build         # render already-built facts
  python -m explorer.newsletter --no-email --out n.html
  python -m explorer.newsletter --to a@x.com --narrate
  python -m explorer.newsletter --issue-date 2026-08-01
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

import db
from .morning_report import HTML_TEMPLATE, MailConfig, send_email, _split_csv

log = logging.getLogger("newsletter")

# Reader-facing section order. Buckets within a section are handled per-section.
SECTION_ORDER = [
    "opening",          # synthesized from market_holidays + earnings_reports
    "index_overview",
    "movers",           # synthesized from movers_advancing + movers_declining
    "sector_heatmap",
    "strategy_picks",
    "ssg_picks",
    "news_recap",
]

# Bucket display order + labels for the two pick sections.
BUCKETS = [("buy", "Buy"), ("hold", "Hold"), ("sell", "Sell")]

POS = "#137333"   # green
NEG = "#c5221f"   # red
NEUT = "#5f6368"  # grey

# Sector heat-map palette: a pale neutral blended toward saturated green/red by
# |return| / HEAT_FULL_SCALE. This is presentation only -- like _color(), it maps
# a SQL-computed figure to a colour; it never computes a figure. Tune the scale
# (the |1d %| at which a cell is fully saturated) to taste.
HEAT_FULL_SCALE = 3.0
_HEAT_BASE = (240, 243, 246)   # #f0f3f6 pale grey
_HEAT_POS = (19, 115, 51)      # POS
_HEAT_NEG = (197, 34, 31)      # NEG
HEAT_COLS = 3                  # sector cells per row in the grid


# --------------------------------------------------------------------------- #
# Formatting helpers (defensive: facts may carry nulls)
# --------------------------------------------------------------------------- #
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _money(v) -> str:
    f = _f(v)
    return "n/a" if f is None else f"{f:,.2f}"


def _pct(v, signed: bool = False) -> str:
    """Format a value that is already a percentage number (e.g. -0.44 -> -0.44%)."""
    f = _f(v)
    if f is None:
        return "n/a"
    return f"{f:+.2f}%" if signed else f"{f:.2f}%"


def _frac_as_pct(v, signed: bool = False) -> str:
    """Format a fraction (0.14 -> 14.0%). Used for SSG returns/yields."""
    f = _f(v)
    if f is None:
        return "n/a"
    return f"{f * 100:+.1f}%" if signed else f"{f * 100:.1f}%"


def _color(v) -> str:
    f = _f(v)
    if f is None or f == 0:
        return NEUT
    return POS if f > 0 else NEG


def _compact_int(v) -> str:
    """Compact a large count for narrow table cells: 62000000 -> '62.0M'."""
    f = _f(v)
    if f is None:
        return "n/a"
    a = abs(f)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{f / div:.1f}{suf}"
    return f"{f:.0f}"


def _heat_color(v, full_scale: float = HEAT_FULL_SCALE) -> tuple[str, str]:
    """Map a signed percent return to (background_hex, text_hex) for a heat cell.

    ``full_scale`` is the |return| at which the cell reaches full saturation.
    Presentation only: it colours a figure, it never derives one.
    """
    f = _f(v)
    if f is None or not full_scale:
        return "#eef2f5", NEUT
    t = max(-1.0, min(1.0, f / full_scale))
    target = _HEAT_POS if t >= 0 else _HEAT_NEG
    mag = abs(t)
    rgb = tuple(round(b + mag * (c - b)) for b, c in zip(_HEAT_BASE, target))
    return "#%02x%02x%02x" % rgb, ("#ffffff" if mag >= 0.55 else "#0b1f33")


def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _items(payload: dict, section: str) -> list[dict]:
    return payload.get(section) or []


def _facts(item: dict) -> dict:
    return item.get("facts") or {}


def _is_no_run(items: list[dict]) -> bool:
    return len(items) == 1 and _facts(items[0]).get("status") == "no_run"


# --------------------------------------------------------------------------- #
# Optional LLM narration (facts-constrained; never computes)
# --------------------------------------------------------------------------- #
def _narrate(section_label: str, facts_blob: str) -> str | None:
    """Ask the model for one short paragraph, constrained to the given facts.
    Returns None on any failure so the caller falls back to deterministic prose.
    """
    try:
        import ollama  # local import: no hard dependency unless --narrate is used
        from explorer.llm import LLMInterface
    except Exception:  # noqa: BLE001
        log.warning("Narration requested but the LLM stack is unavailable; using plain prose.")
        return None

    model = LLMInterface().analysis_model
    prompt = f"""You are writing one short paragraph for a daily market newsletter: {section_label}.

FACTS (the only information you may use):
{facts_blob}

Write 2-4 sentences of plain prose (no headers, no lists, no markdown).
STRICT RULES:
- Use ONLY the names, tickers, and numbers in FACTS. Invent nothing.
- Do not add price targets, dates, or percentages not present in FACTS.
- Refer to each company by the exact name shown; never guess a business from a ticker.
- If a fact is missing, omit it rather than inventing it."""
    try:
        resp = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.3, "num_predict": 220},
        )
        return resp["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("Narration failed (%s); using plain prose.", exc)
        return None


# --------------------------------------------------------------------------- #
# Section renderers -> HTML fragments
# --------------------------------------------------------------------------- #
def _render_opening(payload: dict, narrate: bool) -> str:
    holidays = _items(payload, "market_holidays")
    earnings = _items(payload, "earnings_reports")

    # Holidays sentence
    hol_parts = []
    today_close = next((h for h in holidays if _facts(h).get("is_today")), None)
    upcoming = next((h for h in holidays if not _facts(h).get("is_today")), None)
    if today_close:
        hol_parts.append(f"US markets are closed today for {_esc(_facts(today_close).get('name'))}.")
    else:
        hol_parts.append("US markets are open today.")
    if upcoming:
        uf = _facts(upcoming)
        hol_parts.append(
            f"The next scheduled market holiday is {_esc(uf.get('name'))} on {_esc(uf.get('date'))}.")

    # Earnings sentence
    earn_parts = []
    if earnings:
        reported = [e for e in earnings if _facts(e).get("status") == "reported"]
        scheduled = [e for e in earnings if _facts(e).get("status") == "scheduled"]
        earn_parts.append(
            f"{len(earnings)} name{'s' if len(earnings) != 1 else ''} on today's earnings calendar.")
        bits = []
        for e in reported[:3]:
            ef = _facts(e)
            bits.append(f"{_esc(ef.get('name') or ef.get('ticker'))} reported EPS "
                        f"{_money(ef.get('eps_actual'))} vs {_money(ef.get('eps_estimate'))} "
                        f"({_pct(ef.get('surprise_pct'), signed=True)} surprise)")
        for e in scheduled[:2]:
            ef = _facts(e)
            bits.append(f"{_esc(ef.get('name') or ef.get('ticker'))} reports "
                        f"{_esc((ef.get('session') or 'today')).lower()}")
        if bits:
            earn_parts.append("; ".join(bits) + ".")

    body_text = " ".join(hol_parts + earn_parts)

    if narrate:
        blob_lines = [f"Holidays: {h['headline']}" for h in holidays] + \
                     [f"Earnings: {e['headline']}" for e in earnings]
        narrated = _narrate("the market open (holidays and earnings)", "\n".join(blob_lines))
        if narrated:
            body_text = narrated

    return f"<h2>Market open</h2>\n<p>{_esc(body_text) if not narrate else body_text}</p>"


def _render_index_overview(payload: dict) -> str:
    items = _items(payload, "index_overview")
    if not items:
        return ""
    rows = [
        '<tr><th>Index</th><th>Last</th><th>1d</th><th>1m</th><th>YTD</th></tr>'
    ]
    for it in items:
        f = _facts(it)
        rows.append(
            "<tr>"
            f"<td>{_esc(f.get('display_name'))}</td>"
            f"<td>{_money(f.get('close'))}</td>"
            f'<td style="color:{_color(f.get("ret_1d"))}">{_pct(f.get("ret_1d"), signed=True)}</td>'
            f'<td style="color:{_color(f.get("ret_1m"))}">{_pct(f.get("ret_1m"), signed=True)}</td>'
            f'<td style="color:{_color(f.get("ret_ytd"))}">{_pct(f.get("ret_ytd"), signed=True)}</td>'
            "</tr>"
        )
    return "<h2>Major markets</h2>\n<table>\n" + "\n".join(rows) + "\n</table>"


def _movers_table(items: list[dict]) -> str:
    rows = ['<tr><th>Ticker</th><th>Company</th><th>Last</th>'
            '<th>1d</th><th>Vol</th></tr>']
    for it in items:
        f = _facts(it)
        rows.append(
            "<tr>"
            f"<td><strong>{_esc(f.get('ticker'))}</strong></td>"
            f"<td>{_esc(f.get('name') or f.get('ticker'))}</td>"
            f"<td>{_money(f.get('close'))}</td>"
            f'<td style="color:{_color(f.get("ret_1d"))}">{_pct(f.get("ret_1d"), signed=True)}</td>'
            f"<td>{_compact_int(f.get('volume'))}</td>"
            "</tr>"
        )
    return "<table>\n" + "\n".join(rows) + "\n</table>"


def _render_movers(payload: dict) -> str:
    adv = _items(payload, "movers_advancing")
    dec = _items(payload, "movers_declining")
    if not adv and not dec:
        return ""
    parts = ["<h2>Most active movers</h2>"]
    if adv:
        parts.append(f"<h3>Advancing ({len(adv)})</h3>")
        parts.append(_movers_table(adv))
    if dec:
        parts.append(f"<h3>Declining ({len(dec)})</h3>")
        parts.append(_movers_table(dec))
    return "\n".join(parts)


def _render_sector_heatmap(payload: dict) -> str:
    items = _items(payload, "sector_heatmap")
    if not items:
        return ""
    cells = []
    for it in items:
        f = _facts(it)
        bg, fg = _heat_color(f.get("weighted_ret_1d"))
        breadth = (f"{f.get('n_advancing', 0)}&#9650; / "
                   f"{f.get('n_declining', 0)}&#9660;")
        # width + white border make a gap-separated grid that survives Outlook,
        # which ignores border-spacing. white-space:normal counters the mobile
        # rule that sets nowrap on .content tables.
        cells.append(
            f'<td width="{100 // HEAT_COLS}%" style="background:{bg};color:{fg};'
            'padding:12px;border:2px solid #ffffff;vertical-align:top;'
            'white-space:normal">'
            f'<div style="font-weight:700;font-size:14px">{_esc(f.get("sector"))}</div>'
            f'<div style="font-size:19px;font-weight:700;margin:2px 0">'
            f'{_pct(f.get("weighted_ret_1d"), signed=True)}</div>'
            f'<div style="font-size:12px">{breadth}</div>'
            "</td>"
        )
    grid = ["<tr>" + "".join(cells[i:i + HEAT_COLS]) + "</tr>"
            for i in range(0, len(cells), HEAT_COLS)]
    return ('<h2>Sector heat map</h2>\n'
            '<p style="font-size:13px;color:#5f6368;margin:0 0 8px">'
            'Cap-weighted 1-day return by sector; arrows show advancers / decliners.</p>\n'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:separate;border-spacing:0;margin:8px 0">\n'
            + "\n".join(grid) + "\n</table>")


def _bucketed(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"buy": [], "hold": [], "sell": []}
    for it in items:
        v = _facts(it).get("verdict")
        if v in out:
            out[v].append(it)
    return out


def _render_strategy_line(it: dict) -> str:
    f = _facts(it)
    tkr = _esc(f.get("ticker"))
    name = _esc(f.get("name") or f.get("ticker"))
    if f.get("rated"):
        detail = (f"consensus {_money(f.get('consensus_rating'))}/5 "
                  f"across {f.get('n_ratings', 'n/a')} ratings")
    else:
        detail = "no analyst coverage"
    sigs = f.get("signals") or []
    sig_txt = f", flagged by {f.get('signal_count', len(sigs))} strategies" if sigs else ""
    star = " \u2605" if f.get("is_top_conviction") else ""
    return f"<p><strong>{tkr}</strong>{star} &mdash; {name}. {detail}{sig_txt}.</p>"


def _render_ssg_line(it: dict) -> str:
    f = _facts(it)
    tkr = _esc(f.get("ticker"))
    name = _esc(f.get("name") or f.get("ticker"))
    verdict = f.get("verdict")
    if verdict == "buy":
        ud = _f(f.get("updown_ratio"))
        ud_txt = f"{ud:.1f}:1 up/down, " if ud is not None else ""
        detail = (f"{ud_txt}{_frac_as_pct(f.get('total_return'))} projected return; "
                  f"buy below {_money(f.get('buy_below'))} (now {_money(f.get('current_price'))})")
    elif verdict == "sell":
        detail = (f"price in the sell zone, above {_money(f.get('sell_above'))} "
                  f"(now {_money(f.get('current_price'))})")
    else:
        detail = (f"quality-growth name in the {_esc((f.get('zone') or 'unpriced')).lower()} zone, "
                  f"{_frac_as_pct(f.get('total_return'))} projected return")
    return f"<p><strong>{tkr}</strong> &mdash; {name}. {detail}.</p>"


def _render_picks(payload: dict, section: str, title: str, line_fn) -> str:
    items = _items(payload, section)
    if not items:
        return ""
    if _is_no_run(items):
        return f"<h2>{title}</h2>\n<p><em>{_esc(items[0]['headline'])}</em></p>"

    buckets = _bucketed(items)
    parts = [f"<h2>{title}</h2>"]
    any_rows = False
    for key, label in BUCKETS:
        rows = buckets.get(key) or []
        if not rows:
            continue
        any_rows = True
        parts.append(f"<h3>{label} ({len(rows)})</h3>")
        parts.extend(line_fn(it) for it in rows)
    if not any_rows:
        parts.append("<p><em>No names in any bucket for this issue.</em></p>")
    return "\n".join(parts)


def _render_news(payload: dict, narrate: bool) -> str:
    items = _items(payload, "news_recap")
    if not items:
        return ""
    if narrate:
        blob = "\n".join(f"[{_facts(i).get('tone', 'neutral')}] {i['headline']}" for i in items)
        narrated = _narrate("a recap of the day's financial news", blob)
        if narrated:
            return f"<h2>News recap</h2>\n<p>{narrated}</p>"
    parts = ["<h2>News recap</h2>"]
    for it in items:
        f = _facts(it)
        tone = f.get("tone", "neutral")
        tone_color = POS if tone == "positive" else NEG if tone == "negative" else NEUT
        tkr = f" &middot; {_esc(f.get('ticker'))}" if f.get("ticker") else ""
        title = _esc(it.get("headline"))
        link = f.get("link")
        title_html = f'<a href="{_esc(link)}">{title}</a>' if link else title
        parts.append(
            f'<p><span style="color:{tone_color};font-weight:600">[{tone}]</span> '
            f"{title_html}{tkr}</p>")
    return "\n".join(parts)


def _render_footer_note(payload: dict) -> str:
    prov = _items(payload, "provenance")
    if not prov:
        return ""
    f = _facts(prov[0])
    asof = f.get("price_as_of")
    return (f'<hr><p style="font-size:13px;color:{NEUT}">Prices as of {_esc(asof)}. '
            "Figures are computed in the database; commentary is informational only, "
            "not investment advice.</p>") if asof else ""


# --------------------------------------------------------------------------- #
# Body assembly
# --------------------------------------------------------------------------- #
def render_html_body(payload: dict, issue_date: date, narrate: bool = False) -> str:
    today = issue_date.strftime("%A, %B %d, %Y")
    parts = [f"<h1>Daily Market Newsletter</h1>", f"<p><em>{today}</em></p>"]
    parts.append(_render_opening(payload, narrate))
    parts.append(_render_index_overview(payload))
    parts.append(_render_movers(payload))
    parts.append(_render_sector_heatmap(payload))
    parts.append(_render_picks(payload, "strategy_picks", "Strategy picks", _render_strategy_line))
    parts.append(_render_picks(payload, "ssg_picks", "Stock Selection Guide", _render_ssg_line))
    parts.append(_render_news(payload, narrate))
    parts.append(_render_footer_note(payload))
    return "\n".join(p for p in parts if p)


def render_text_body(payload: dict, issue_date: date) -> str:
    """Plain-text fallback assembled from the deterministic headlines."""
    lines = [f"DAILY MARKET NEWSLETTER -- {issue_date.isoformat()}", ""]
    labels = {
        "market_holidays": "MARKET HOLIDAYS", "earnings_reports": "EARNINGS TODAY",
        "index_overview": "MAJOR MARKETS",
        "movers_advancing": "MOST ACTIVE -- ADVANCING",
        "movers_declining": "MOST ACTIVE -- DECLINING",
        "sector_heatmap": "SECTOR HEAT MAP", "strategy_picks": "STRATEGY PICKS",
        "ssg_picks": "STOCK SELECTION GUIDE", "news_recap": "NEWS RECAP",
    }
    for section in ["market_holidays", "earnings_reports", "index_overview",
                    "movers_advancing", "movers_declining", "sector_heatmap",
                    "strategy_picks", "ssg_picks", "news_recap"]:
        items = _items(payload, section)
        if not items:
            continue
        lines.append(labels[section])
        for it in items:
            verdict = _facts(it).get("verdict")
            prefix = f"[{verdict.upper()}] " if verdict else ""
            lines.append(f"  - {prefix}{it.get('headline', '')}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def load_payload(issue_date: date, do_build: bool = True) -> dict:
    if do_build:
        log.info("Building newsletter facts for %s ...", issue_date)
        db.execute("SELECT build_newsletter(%s)", (issue_date,))
    row = db.fetch_one_ro("SELECT newsletter_payload(%s) AS payload", (issue_date,))
    payload = (row or {}).get("payload")
    return payload or {}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build and email the daily market newsletter.")
    p.add_argument("--to", help="Comma-separated recipients (overrides MAIL_TO).")
    p.add_argument("--out", help="Also write the generated HTML to this path.")
    p.add_argument("--no-email", action="store_true", help="Build/render only; print, don't send.")
    p.add_argument("--no-build", action="store_true",
                   help="Do not recompute facts; render the already-built issue.")
    p.add_argument("--narrate", action="store_true",
                   help="Have the local LLM narrate the opening and news sections.")
    p.add_argument("--issue-date", help="ISO date (YYYY-MM-DD); default today.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    issue_date = (datetime.strptime(args.issue_date, "%Y-%m-%d").date()
                  if args.issue_date else date.today())

    payload = load_payload(issue_date, do_build=not args.no_build)
    if not payload:
        log.error("Empty payload for %s -- nothing to send.", issue_date)
        return 1

    html_inner = render_html_body(payload, issue_date, narrate=args.narrate)
    subject = f"Daily Market Newsletter -- {issue_date.strftime('%A, %B %d, %Y')}"
    html_body = HTML_TEMPLATE.format(title=subject, body=html_inner)
    text_body = render_text_body(payload, issue_date)

    if args.out:
        from pathlib import Path
        Path(args.out).write_text(html_body, encoding="utf-8")
        log.info("Wrote HTML to %s", args.out)

    if args.no_email:
        print(html_body)
        return 0

    recipients = _split_csv(args.to) if args.to else None
    cfg = MailConfig.from_env(recipients)
    send_email(cfg, subject, html_body, text_body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
