#!/usr/bin/env python3
"""
newsletter.py -- Build and email the daily market newsletter.

This is the renderer for the grounded-facts newsletter layer. All the numbers
are computed in SQL by ``build_newsletter()`` (sql/02_newsletter_findings.sql)
and exposed as one JSON document by ``newsletter_payload()``. This module reads
that document and lays it out, in a fixed reader-facing order:

  1. Opening    -- market holidays (if any) and today's earnings reports
  2. Major markets -- the benchmark indices (DOW, S&P, Nasdaq, ...)
  3. Strategy picks -- explorer signals, bucketed Buy / Hold / Sell
  4. Stock Selection Guide -- SSG names, bucketed Buy / Hold / Sell
  5. News recap -- the day's financial headlines

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
    "strategy_picks",
    "ssg_picks",
    "news_recap",
]

# Bucket display order + labels for the two pick sections.
BUCKETS = [("buy", "Buy"), ("hold", "Hold"), ("sell", "Sell")]

POS = "#137333"   # green
NEG = "#c5221f"   # red
NEUT = "#5f6368"  # grey


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
        "index_overview": "MAJOR MARKETS", "strategy_picks": "STRATEGY PICKS",
        "ssg_picks": "STOCK SELECTION GUIDE", "news_recap": "NEWS RECAP",
    }
    for section in ["market_holidays", "earnings_reports", "index_overview",
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
