"""
app.py
------
Dash application: stock charting + fundamentals + portfolio CRUD.

Run with:
    python app.py
or in production:
    gunicorn app:server -b 0.0.0.0:8050

Pages:
  * /         - landing
  * /chart    - chart + fundamentals dashboard for a single ticker
  * /portfolios - list / create / edit / delete portfolios
  * /portfolios/<id> - portfolio detail with trades CRUD
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import dash
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, callback_context, dcc, html, no_update
from dash import dash_table
from dash.exceptions import PreventUpdate
from plotly.subplots import make_subplots

import portfolio as pf
from config import settings
from db import fetch_all, fetch_one
from ingest import Ingestor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("app")

# ---------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------
app = Dash(
    __name__,
    title="EODHD Charting + Portfolio Tracker",
    suppress_callback_exceptions=True,
    external_stylesheets=[
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css",
    ],
)
server = app.server  # for gunicorn
ingestor = Ingestor()


# ---------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------
def load_prices(ticker: str, days: int = 365 * 3) -> pd.DataFrame:
    rows = fetch_all(
        """SELECT date, open, high, low, close, adjusted_close, volume
             FROM eod_prices
            WHERE ticker = %s
              AND date >= %s
         ORDER BY date""",
        (ticker, date.today() - timedelta(days=days)),
    )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        for c in ("open", "high", "low", "close", "adjusted_close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df


def load_fundamentals(ticker: str) -> dict | None:
    return fetch_one("SELECT * FROM fundamentals WHERE ticker = %s", (ticker,))


def load_dividends(ticker: str) -> pd.DataFrame:
    rows = fetch_all(
        "SELECT ex_date, value, currency FROM dividends "
        "WHERE ticker=%s ORDER BY ex_date DESC LIMIT 40",
        (ticker,),
    )
    return pd.DataFrame(rows)


def load_news(ticker: str, limit: int = 20) -> pd.DataFrame:
    rows = fetch_all(
        """SELECT published_at, title, link, sentiment_polarity
             FROM news
            WHERE ticker = %s OR %s = ANY(symbols)
         ORDER BY published_at DESC
            LIMIT %s""",
        (ticker, ticker, limit),
    )
    return pd.DataFrame(rows)


def load_financials(ticker: str, statement: str = "income",
                    period_type: str = "yearly") -> pd.DataFrame:
    table = {
        "income": "income_statements",
        "balance": "balance_sheets",
        "cashflow": "cash_flow_statements",
    }[statement]
    rows = fetch_all(
        f"SELECT * FROM {table} WHERE ticker=%s AND period_type=%s "
        f"ORDER BY date DESC LIMIT 10",
        (ticker, period_type),
    )
    return pd.DataFrame(rows)


def load_ssg_history(ticker: str, years: int = 10) -> pd.DataFrame:
    """Annual sales, net income and EPS for the NAIC Stock Selection Guide.

    Sales and net income come from annual income statements. Annual EPS is
    summed from per-quarter earnings_history rows (falling back to
    net_income / implied share count when EPS isn't reported). Returns a
    DataFrame indexed oldest->newest with columns:
        year, sales, net_income, eps
    Rows with no usable data are dropped; the frame may be shorter than
    `years` (or empty) for thinly-covered tickers.
    """
    inc = pd.DataFrame(fetch_all(
        """SELECT date, total_revenue, net_income
             FROM income_statements
            WHERE ticker=%s AND period_type='yearly'
         ORDER BY date DESC LIMIT %s""",
        (ticker, years),
    ))
    eps = pd.DataFrame(fetch_all(
        """SELECT date, eps_actual
             FROM earnings_history
            WHERE ticker=%s AND eps_actual IS NOT NULL
         ORDER BY date DESC LIMIT %s""",
        (ticker, years * 4 + 4),
    ))

    if inc.empty:
        return pd.DataFrame(columns=["year", "sales", "net_income", "eps"])

    inc["year"] = pd.to_datetime(inc["date"], errors="coerce").dt.year
    inc["sales"] = pd.to_numeric(inc["total_revenue"], errors="coerce")
    inc["net_income"] = pd.to_numeric(inc["net_income"], errors="coerce")

    # Annual EPS: sum the four quarterly actuals reported within each fiscal year.
    eps_by_year: dict[int, float] = {}
    if not eps.empty:
        eps["year"] = pd.to_datetime(eps["date"], errors="coerce").dt.year
        eps["eps_actual"] = pd.to_numeric(eps["eps_actual"], errors="coerce")
        grp = eps.dropna(subset=["year"]).groupby("year")
        for yr, g in grp:
            # Only trust a year that looks like it has full coverage (>=3 qtrs);
            # otherwise leave it to the net-income fallback below.
            if g["eps_actual"].notna().sum() >= 3:
                eps_by_year[int(yr)] = float(g["eps_actual"].sum())

    out = (inc[["year", "sales", "net_income"]]
           .dropna(subset=["year"])
           .groupby("year", as_index=False)
           .agg({"sales": "max", "net_income": "max"}))
    out["eps"] = out["year"].map(lambda y: eps_by_year.get(int(y)))
    out = out.sort_values("year").reset_index(drop=True)
    # Keep rows that have at least sales or eps to plot.
    out = out[out[["sales", "eps"]].notna().any(axis=1)]
    return out.reset_index(drop=True)


def search_tickers(q: str, limit: int = 30) -> list[dict]:
    if not q:
        return []
    rows = fetch_all(
        """SELECT ticker, name FROM symbols
            WHERE ticker ILIKE %s OR name ILIKE %s
         ORDER BY (ticker = %s) DESC,
                  (ticker ILIKE %s) DESC,
                  length(ticker) ASC
            LIMIT %s""",
        (f"{q}%", f"%{q}%", q.upper(), f"{q}%", limit),
    )
    return [{"label": f"{r['ticker']} – {r['name'] or ''}", "value": r["ticker"]}
            for r in rows]


# ---------------------------------------------------------------------
# Layout: shell
# ---------------------------------------------------------------------
def nav() -> html.Nav:
    return html.Nav(
        className="navbar navbar-expand-lg navbar-dark bg-dark px-3",
        children=[
            html.A("EODHD Charting", className="navbar-brand", href="/"),
            html.Div([
                dcc.Link("Chart", href="/chart",
                         className="nav-link text-white mx-2"),
                dcc.Link("Portfolios", href="/portfolios",
                         className="nav-link text-white mx-2"),
            ], className="navbar-nav d-flex flex-row"),
        ],
    )


app.layout = html.Div([
    dcc.Location(id="url"),
    nav(),
    html.Div(id="page-content", className="container-fluid p-4"),
    dcc.Store(id="trade-edit-target"),
    dcc.Store(id="portfolio-edit-target"),
])


# ---------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------
def landing_page() -> html.Div:
    return html.Div([
        html.H2("Welcome"),
        html.P("Pull data from EODHD into Postgres, then chart and track "
               "portfolios here."),
        html.Ul([
            html.Li([dcc.Link("Chart a ticker", href="/chart"),
                     " — OHLC + indicators + fundamentals."]),
            html.Li([dcc.Link("Portfolios", href="/portfolios"),
                     " — create, edit, and delete portfolios and trades."]),
        ]),
        html.Hr(),
        html.H5("Quick ingest"),
        html.P("Type a ticker (AAPL.US), or a company name / partial symbol "
               "and look it up.", className="text-muted small mb-2"),
        html.Div([
            dcc.Input(id="ingest-query", placeholder="AAPL.US  or  \"Apple\"",
                      type="text", debounce=True,
                      className="form-control d-inline-block w-25 me-2"),
            html.Button("Look up", id="ingest-lookup-btn",
                        className="btn btn-outline-secondary me-2"),
            html.Button("Ingest everything", id="ingest-btn",
                        className="btn btn-primary"),
            html.Span(id="ingest-status", className="ms-3"),
        ], className="d-flex align-items-center flex-wrap"),
        html.Div([
            dcc.Dropdown(
                id="ingest-match",
                placeholder="Lookup matches will appear here — pick one",
                style={"width": "520px"},
                className="mt-2",
            ),
        ], id="ingest-match-wrap", style={"display": "none"}),
        # Remembers the resolved ticker chosen/looked-up for the ingest click.
        dcc.Store(id="ingest-resolved"),
    ])


def chart_page(ticker: str = "AAPL.US") -> html.Div:
    return html.Div([
        html.Div([
            html.Label("Ticker", className="me-2 fw-bold"),
            dcc.Dropdown(
                id="chart-ticker",
                value=ticker,
                options=search_tickers(ticker) or [{"label": ticker, "value": ticker}],
                style={"width": "320px", "display": "inline-block"},
                placeholder="Start typing ticker or name…",
                searchable=True,
            ),
            html.Label("Range", className="ms-3 me-2"),
            dcc.Dropdown(
                id="chart-range",
                options=[
                    {"label": "1M", "value": 30},
                    {"label": "3M", "value": 90},
                    {"label": "6M", "value": 180},
                    {"label": "1Y", "value": 365},
                    {"label": "3Y", "value": 365 * 3},
                    {"label": "5Y", "value": 365 * 5},
                    {"label": "Max", "value": 365 * 30},
                ],
                value=365,
                clearable=False,
                style={"width": "120px", "display": "inline-block"},
            ),
            html.Label("Chart type", className="ms-3 me-2"),
            dcc.Dropdown(
                id="chart-type",
                options=[{"label": v, "value": v} for v in
                         ("Candlestick", "OHLC", "Line", "Area")],
                value="Candlestick", clearable=False,
                style={"width": "150px", "display": "inline-block"},
            ),
            dcc.Checklist(
                id="chart-overlays",
                options=[
                    {"label": " SMA20", "value": "sma20"},
                    {"label": " SMA50", "value": "sma50"},
                    {"label": " SMA200", "value": "sma200"},
                    {"label": " Bollinger(20,2)", "value": "bb"},
                    {"label": " Volume", "value": "vol"},
                ],
                value=["sma50", "vol"],
                inline=True,
                style={"display": "inline-block", "marginLeft": "12px"},
            ),
            dcc.Checklist(
                id="chart-show-ssg",
                options=[{"label": " Stock Selection Guide (SSG)",
                          "value": "ssg"}],
                value=[],
                inline=True,
                style={"display": "inline-block", "marginLeft": "12px"},
            ),
        ], className="d-flex align-items-center flex-wrap mb-3"),

        dcc.Loading(dcc.Graph(id="price-chart", style={"height": "620px"})),

        # NAIC Stock Selection Guide (rendered only when the box is checked)
        dcc.Loading(html.Div(id="ssg-panel", className="my-3")),

        # Fundamentals header card
        html.Div(id="fundamentals-header", className="my-3"),

        dcc.Tabs(id="fund-tabs", value="overview", children=[
            dcc.Tab(label="Overview",            value="overview"),
            dcc.Tab(label="Income Statement",    value="income"),
            dcc.Tab(label="Balance Sheet",       value="balance"),
            dcc.Tab(label="Cash Flow",           value="cashflow"),
            dcc.Tab(label="Valuation",           value="valuation"),
            dcc.Tab(label="Earnings",            value="earnings"),
            dcc.Tab(label="Dividends & Splits",  value="divsplits"),
            dcc.Tab(label="Holders",             value="holders"),
            dcc.Tab(label="Insider Trades",      value="insider"),
            dcc.Tab(label="Analyst Ratings",     value="ratings"),
            dcc.Tab(label="ESG",                 value="esg"),
            dcc.Tab(label="News",                value="news"),
        ]),
        html.Div(id="fund-tab-content", className="mt-3"),
    ])


def portfolios_page() -> html.Div:
    portfolios = pf.list_portfolios()
    return html.Div([
        html.Div([
            html.H2("Portfolios", className="d-inline-block"),
            html.Button("+ New Portfolio", id="new-portfolio-btn",
                        className="btn btn-primary float-end"),
        ]),
        html.Div(id="portfolio-form-area", className="my-3"),
        html.Div(id="portfolio-toast", className="my-2"),
        dash_table.DataTable(
            id="portfolios-table",
            columns=[
                {"name": "Name",          "id": "name"},
                {"name": "Description",   "id": "description"},
                {"name": "Base ccy",      "id": "base_currency"},
                {"name": "Initial cash",  "id": "initial_cash",
                 "type": "numeric", "format": {"specifier": ",.2f"}},
                {"name": "Trades",        "id": "trade_count", "type": "numeric"},
                {"name": "Tickers",       "id": "ticker_count", "type": "numeric"},
                {"name": "Invested",      "id": "gross_invested",
                 "type": "numeric", "format": {"specifier": ",.2f"}},
                {"name": "Proceeds",      "id": "gross_proceeds",
                 "type": "numeric", "format": {"specifier": ",.2f"}},
                {"name": "Open",          "id": "open_link", "presentation": "markdown"},
                {"name": "Edit",          "id": "edit_link", "presentation": "markdown"},
                {"name": "Delete",        "id": "delete_link", "presentation": "markdown"},
            ],
            data=[
                {
                    **{k: _serialise(v) for k, v in p.items()},
                    "open_link": f"[open](/portfolios/{p['id']})",
                    "edit_link": f"[edit](#edit-{p['id']})",
                    "delete_link": f"[delete](#delete-{p['id']})",
                }
                for p in portfolios
            ],
            style_table={"overflowX": "auto"},
            style_cell={"padding": "6px", "fontFamily": "system-ui"},
            page_size=20,
            markdown_options={"link_target": "_self"},
        ),
    ])


def portfolio_detail_page(portfolio_id: str) -> html.Div:
    p = pf.get_portfolio(portfolio_id)
    if not p:
        return html.Div([html.H3("Portfolio not found"),
                         dcc.Link("Back", href="/portfolios")])
    trades = pf.list_trades(portfolio_id)
    positions = pf.positions(portfolio_id)
    return html.Div([
        dcc.Store(id="pid", data=_serialise(portfolio_id)),
        dcc.Link("← All portfolios", href="/portfolios"),
        html.H3(p["name"], className="mt-2"),
        html.P(p.get("description") or ""),

        html.Div([
            _stat("Trades", p.get("trade_count") or 0),
            _stat("Tickers", p.get("ticker_count") or 0),
            _stat("Invested", f"{p.get('gross_invested') or 0:,.2f}"),
            _stat("Proceeds", f"{p.get('gross_proceeds') or 0:,.2f}"),
            _stat("Base ccy", p.get("base_currency") or ""),
        ], className="d-flex flex-wrap gap-3 my-3"),

        # Trades section
        html.H5("Trades"),
        html.Div(id="trade-form-area", className="my-3"),
        html.Button("+ Add Trade", id="new-trade-btn", className="btn btn-success mb-2"),
        html.Div(id="trade-toast", className="my-2"),
        dash_table.DataTable(
            id="trades-table",
            columns=[
                {"name": "Date",      "id": "trade_date"},
                {"name": "Ticker",    "id": "ticker"},
                {"name": "Side",      "id": "side"},
                {"name": "Qty",       "id": "quantity",
                 "type": "numeric", "format": {"specifier": ",.4f"}},
                {"name": "Price",     "id": "price",
                 "type": "numeric", "format": {"specifier": ",.4f"}},
                {"name": "Fees",      "id": "fees",
                 "type": "numeric", "format": {"specifier": ",.2f"}},
                {"name": "Ccy",       "id": "currency"},
                {"name": "Notes",     "id": "notes"},
                {"name": "Edit",      "id": "edit_link",   "presentation": "markdown"},
                {"name": "Delete",    "id": "delete_link", "presentation": "markdown"},
            ],
            data=[
                {**{k: _serialise(v) for k, v in t.items()},
                 "edit_link":   f"[edit](#etrade-{t['id']})",
                 "delete_link": f"[delete](#dtrade-{t['id']})"}
                for t in trades
            ],
            style_table={"overflowX": "auto"},
            style_cell={"padding": "6px"},
            page_size=25,
            markdown_options={"link_target": "_self"},
        ),

        html.H5("Current Positions", className="mt-4"),
        dash_table.DataTable(
            columns=[
                {"name": "Ticker",     "id": "ticker"},
                {"name": "Name",       "id": "symbol_name"},
                {"name": "Sector",     "id": "sector"},
                {"name": "Quantity",   "id": "quantity",
                 "type": "numeric", "format": {"specifier": ",.4f"}},
                {"name": "Avg cost",   "id": "avg_buy_price",
                 "type": "numeric", "format": {"specifier": ",.4f"}},
                {"name": "Cost basis", "id": "cost_basis",
                 "type": "numeric", "format": {"specifier": ",.2f"}},
                {"name": "Last px",    "id": "last_price",
                 "type": "numeric", "format": {"specifier": ",.4f"}},
                {"name": "Market val", "id": "market_value",
                 "type": "numeric", "format": {"specifier": ",.2f"}},
                {"name": "Unrealised", "id": "unrealised_pnl",
                 "type": "numeric", "format": {"specifier": ",.2f"}},
                {"name": "P&L %",      "id": "unrealised_pnl_pct",
                 "type": "numeric", "format": {"specifier": ",.2f"}},
            ],
            data=[{k: _serialise(v) for k, v in r.items()} for r in positions],
            style_data_conditional=[
                {"if": {"filter_query": "{unrealised_pnl} > 0", "column_id": "unrealised_pnl"},
                 "color": "#1b7d3b"},
                {"if": {"filter_query": "{unrealised_pnl} < 0", "column_id": "unrealised_pnl"},
                 "color": "#b1281d"},
            ],
            style_table={"overflowX": "auto"},
            style_cell={"padding": "6px"},
        ),

        html.H5("Equity curve (marked-to-market)", className="mt-4"),
        dcc.Graph(id="equity-curve", figure=_equity_curve_fig(portfolio_id)),
    ])


def _stat(label: str, value: Any) -> html.Div:
    return html.Div([
        html.Div(label, className="text-muted small"),
        html.Div(str(value), className="fw-bold fs-5"),
    ], className="border rounded p-2 px-3 bg-light")


def _serialise(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    # Catch anything else Dash can't JSON-serialise (e.g. memoryview,
    # bytes, IP addresses) by stringifying as a last resort.
    if v is not None and not isinstance(v, (str, int, float, bool, list, dict)):
        return str(v)
    return v


# ---------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------
@app.callback(Output("page-content", "children"), Input("url", "pathname"))
def route(pathname: str):
    if not pathname or pathname == "/":
        return landing_page()
    if pathname.startswith("/chart"):
        return chart_page()
    if pathname == "/portfolios":
        return portfolios_page()
    if pathname.startswith("/portfolios/"):
        pid = pathname.split("/portfolios/", 1)[1]
        return portfolio_detail_page(pid)
    return html.Div([html.H3("Not found"),
                     dcc.Link("Home", href="/")])


# ---------------------------------------------------------------------
# Ingest on landing
# ---------------------------------------------------------------------
@app.callback(
    Output("ingest-match", "options"),
    Output("ingest-match-wrap", "style"),
    Output("ingest-status", "children", allow_duplicate=True),
    Input("ingest-lookup-btn", "n_clicks"),
    State("ingest-query", "value"),
    prevent_initial_call=True,
)
def lookup_ticker(_clicks: int, query: str):
    """Resolve a free-text query to candidate instruments via the Search API."""
    query = (query or "").strip()
    if not query:
        return [], {"display": "none"}, "Enter a ticker or company name first."
    try:
        hits = ingestor.search_symbols(query, limit=20)
    except Exception as e:  # noqa: BLE001
        log.exception("lookup failed")
        return [], {"display": "none"}, html.Span(f"✗ {e}", className="text-danger")
    if not hits:
        return ([], {"display": "none"},
                html.Span(f"No matches for “{query}”.", className="text-warning"))
    opts = [
        {
            "label": f"{h['ticker']} — {h['name'] or ''}"
                     f"  ({h['type'] or '?'}, {h['country'] or '?'})",
            "value": h["ticker"],
        }
        for h in hits
    ]
    msg = html.Span(f"Found {len(opts)} match(es) — pick one, then Ingest.",
                    className="text-muted")
    return opts, {"display": "block"}, msg


@app.callback(
    Output("ingest-resolved", "data"),
    Input("ingest-match", "value"),
    prevent_initial_call=True,
)
def pick_match(ticker: str):
    return ticker or no_update


@app.callback(
    Output("ingest-status", "children"),
    Input("ingest-btn", "n_clicks"),
    State("ingest-query", "value"),
    State("ingest-resolved", "data"),
    prevent_initial_call=True,
)
def trigger_ingest(_clicks: int, query: str, resolved: str | None):
    # Priority: an explicit lookup match, else resolve the typed query.
    ticker = (resolved or "").strip()
    raw = (query or "").strip()
    try:
        if not ticker:
            if not raw:
                return "Enter a ticker or company name first."
            ticker = ingestor.resolve_ticker(raw)
            if not ticker:
                return html.Span(
                    f"Couldn't resolve “{raw}”. Try Look up to see matches.",
                    className="text-warning")
        ingestor.ingest_all_for_ticker(ticker)
        return html.Span(f"✓ Ingested {ticker}", className="text-success")
    except Exception as e:  # noqa: BLE001
        log.exception("ingest failed")
        return html.Span(f"✗ {e}", className="text-danger")


# ---------------------------------------------------------------------
# Charting callbacks
# ---------------------------------------------------------------------
@app.callback(
    Output("chart-ticker", "options"),
    Input("chart-ticker", "search_value"),
)
def update_ticker_options(q: str):
    if not q:
        raise PreventUpdate
    return search_tickers(q)


@app.callback(
    Output("price-chart", "figure"),
    Input("chart-ticker", "value"),
    Input("chart-range", "value"),
    Input("chart-type", "value"),
    Input("chart-overlays", "value"),
)
def render_chart(ticker: str, days: int, chart_type: str, overlays: list[str]):
    if not ticker:
        return go.Figure()
    try:
        df = load_prices(ticker, days=days)
        if df.empty:
            fig = go.Figure()
            fig.add_annotation(
                text=f"No price data for {ticker}. Use the ingest button on the home page.",
                xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            )
            return fig

        overlays = overlays or []
        has_vol = "vol" in overlays

        fig = make_subplots(
            rows=2 if has_vol else 1, cols=1, shared_xaxes=True,
            row_heights=[0.78, 0.22] if has_vol else [1],
            vertical_spacing=0.03,
        )

        if chart_type == "Candlestick":
            fig.add_trace(go.Candlestick(
                x=df["date"], open=df["open"], high=df["high"],
                low=df["low"], close=df["close"], name=ticker,
            ), row=1, col=1)
        elif chart_type == "OHLC":
            fig.add_trace(go.Ohlc(
                x=df["date"], open=df["open"], high=df["high"],
                low=df["low"], close=df["close"], name=ticker,
            ), row=1, col=1)
        elif chart_type == "Area":
            fig.add_trace(go.Scatter(x=df["date"], y=df["close"], fill="tozeroy",
                                     mode="lines", name="Close"), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(x=df["date"], y=df["close"],
                                     mode="lines", name="Close"), row=1, col=1)

        if "sma20" in overlays:
            fig.add_trace(go.Scatter(x=df["date"], y=df["close"].rolling(20).mean(),
                                     mode="lines", name="SMA20",
                                     line=dict(width=1)), row=1, col=1)
        if "sma50" in overlays:
            fig.add_trace(go.Scatter(x=df["date"], y=df["close"].rolling(50).mean(),
                                     mode="lines", name="SMA50",
                                     line=dict(width=1)), row=1, col=1)
        if "sma200" in overlays:
            fig.add_trace(go.Scatter(x=df["date"], y=df["close"].rolling(200).mean(),
                                     mode="lines", name="SMA200",
                                     line=dict(width=1)), row=1, col=1)
        if "bb" in overlays:
            m = df["close"].rolling(20).mean()
            s = df["close"].rolling(20).std()
            fig.add_trace(go.Scatter(x=df["date"], y=m + 2 * s, mode="lines",
                                     name="BB upper", line=dict(width=1, dash="dot")),
                          row=1, col=1)
            fig.add_trace(go.Scatter(x=df["date"], y=m - 2 * s, mode="lines",
                                     name="BB lower", line=dict(width=1, dash="dot"),
                                     fill="tonexty", fillcolor="rgba(0,100,200,0.05)"),
                          row=1, col=1)

        if has_vol:
            # Green when the close rose vs the previous day, red when it fell.
            # The first bar has no prior day to compare against, so it's neutral.
            close_change = df["close"].diff()
            up = "rgba(38,166,154,0.6)"     # green
            down = "rgba(239,83,80,0.6)"    # red
            neutral = "rgba(120,120,120,0.5)"
            vol_colors = [
                neutral if pd.isna(c) else (up if c >= 0 else down)
                for c in close_change
            ]
            fig.add_trace(go.Bar(x=df["date"], y=df["volume"], name="Volume",
                                 marker=dict(color=vol_colors)),
                          row=2, col=1)

        fig.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            xaxis_rangeslider_visible=False,
            legend=dict(orientation="h", y=1.05),
            hovermode="x unified",
            template="plotly_white",
        )
        return fig
    except Exception as e:  # noqa: BLE001
        log.exception("render_chart(ticker=%r) failed", ticker)
        fig = go.Figure()
        fig.add_annotation(
            text=f"Chart failed: {type(e).__name__}: {e}",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
        )
        return fig


@app.callback(
    Output("fundamentals-header", "children"),
    Input("chart-ticker", "value"),
)
def render_fundamentals_header(ticker: str):
    if not ticker:
        return None
    f = load_fundamentals(ticker)
    if not f:
        return html.Div(
            f"No fundamentals stored for {ticker}. "
            "Run ingest from the home page.",
            className="alert alert-warning",
        )
    return html.Div([
        html.Div([
            html.H3(f.get("name") or ticker, className="d-inline-block me-2"),
            html.Span(ticker, className="badge bg-secondary me-2"),
            html.Span(f.get("asset_type") or "", className="badge bg-info me-2"),
            html.Span(f.get("sector") or "", className="badge bg-light text-dark me-2"),
            html.Span(f.get("industry") or "", className="badge bg-light text-dark me-2"),
            html.Span(f.get("country") or "", className="badge bg-light text-dark"),
        ]),
        html.Div([
            _stat("Market cap", f"{_fmt_big(f.get('market_cap'))}"),
            _stat("P/E", _fmt_num(f.get("pe_ratio"))),
            _stat("EPS", _fmt_num(f.get("eps"))),
            _stat("Div yield", _fmt_pct(f.get("dividend_yield"))),
            _stat("Profit margin", _fmt_pct(f.get("profit_margin"))),
            _stat("ROE", _fmt_pct(f.get("return_on_equity"))),
            _stat("Revenue TTM", _fmt_big(f.get("revenue_ttm"))),
            _stat("Employees", f"{f.get('full_time_employees') or '–':,}"
                  if f.get('full_time_employees') else "–"),
        ], className="d-flex flex-wrap gap-2 my-2"),
        html.P(f.get("description") or "", className="text-muted small",
               style={"maxHeight": "120px", "overflow": "auto"}),
    ])


def _fmt_num(v):
    if v is None or isinstance(v, (pd.DataFrame, pd.Series)):
        return "–"
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "–"


def _fmt_pct(v):
    if v is None or isinstance(v, (pd.DataFrame, pd.Series)):
        return "–"
    try:
        return f"{float(v) * 100:,.2f}%"
    except (TypeError, ValueError):
        return "–"


def _fmt_big(v):
    if v is None or isinstance(v, (pd.DataFrame, pd.Series)):
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    for unit, threshold in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= threshold:
            return f"{v/threshold:,.2f}{unit}"
    return f"{v:,.2f}"


@app.callback(
    Output("fund-tab-content", "children"),
    Input("fund-tabs", "value"),
    Input("chart-ticker", "value"),
)
def render_fund_tab(tab: str, ticker: str):
    if not ticker:
        return None
    try:
        f = load_fundamentals(ticker)
        if not f:
            return html.Div("No fundamentals stored.")

        if tab == "overview":
            return _fund_overview(f)
        if tab in ("income", "balance", "cashflow"):
            return _fund_statement_view(ticker, tab)
        if tab == "valuation":
            return _json_table(f.get("valuation"))
        if tab == "earnings":
            return _earnings_view(ticker, f)
        if tab == "divsplits":
            return _divsplits_view(ticker)
        if tab == "holders":
            return _holders_view(ticker)
        if tab == "insider":
            return _insider_view(ticker)
        if tab == "ratings":
            return _json_table(f.get("analyst_ratings"))
        if tab == "esg":
            return _json_table(f.get("esg_scores"))
        if tab == "news":
            return _news_view(ticker)
        return None
    except Exception as e:  # noqa: BLE001
        log.exception("render_fund_tab(tab=%r, ticker=%r) failed", tab, ticker)
        return html.Div([
            html.H6(f"Error rendering {tab!r}", className="text-danger"),
            html.Pre(f"{type(e).__name__}: {e}"),
            html.P("Full traceback is in the server log.", className="text-muted small"),
        ], className="alert alert-danger")


@app.callback(
    Output("ssg-panel", "children"),
    Input("chart-show-ssg", "value"),
    Input("chart-ticker", "value"),
)
def render_ssg(show: list[str], ticker: str):
    if not show or "ssg" not in show:
        return None
    if not ticker:
        return html.Div("Pick a ticker to see its SSG.", className="text-muted")
    try:
        f = load_fundamentals(ticker)
        return build_ssg(ticker, f)
    except Exception as e:  # noqa: BLE001
        log.exception("render_ssg(ticker=%r) failed", ticker)
        return html.Div([
            html.H6("Error building SSG", className="text-danger"),
            html.Pre(f"{type(e).__name__}: {e}"),
        ], className="alert alert-danger")


def _fund_overview(f: dict) -> html.Div:
    rows = []
    field_map = [
        ("Sector", "sector"), ("Industry", "industry"),
        ("Country", "country"), ("Currency", "currency"),
        ("Exchange (primary)", "primary_ticker"),
        ("ISIN", "isin"), ("CIK", "cik"),
        ("IPO date", "ipo_date"), ("Fiscal year end", "fiscal_year_end"),
        ("Web URL", "web_url"),
        ("Market cap", "market_cap"), ("EBITDA", "ebitda"),
        ("P/E", "pe_ratio"), ("PEG", "peg_ratio"),
        ("EPS", "eps"), ("Book value", "book_value"),
        ("Dividend / share", "dividend_share"),
        ("Dividend yield", "dividend_yield"),
        ("Profit margin", "profit_margin"),
        ("Operating margin", "operating_margin"),
        ("ROA", "return_on_assets"), ("ROE", "return_on_equity"),
        ("Revenue TTM", "revenue_ttm"),
        ("Gross profit TTM", "gross_profit_ttm"),
        ("Q rev growth (YoY)", "quarterly_revenue_growth"),
        ("Q earnings growth (YoY)", "quarterly_earnings_growth"),
        ("WS target price", "wall_street_target_price"),
    ]
    for label, key in field_map:
        v = f.get(key)
        if isinstance(v, (pd.DataFrame, pd.Series)):
            # Should never happen, but be defensive against the
            # "truth value of a DataFrame is ambiguous" bug class.
            continue
        if v is None or v == "":
            continue
        if key.endswith("_ttm") or key in ("market_cap", "ebitda"):
            v = _fmt_big(v)
        elif key in ("dividend_yield", "profit_margin", "operating_margin",
                     "return_on_assets", "return_on_equity",
                     "quarterly_revenue_growth", "quarterly_earnings_growth"):
            v = _fmt_pct(v)
        elif isinstance(v, (int, float, Decimal)):
            v = _fmt_num(v)
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        rows.append({"Field": label, "Value": str(v)})
    return dash_table.DataTable(
        data=rows,
        columns=[{"name": c, "id": c} for c in ("Field", "Value")],
        style_cell={"padding": "6px", "textAlign": "left"},
        style_table={"maxWidth": "700px"},
    )


def _fund_statement_view(ticker: str, statement: str) -> html.Div:
    yearly = load_financials(ticker, statement, "yearly")
    if yearly.empty:
        return html.Div("No data.")
    cols = [c for c in yearly.columns
            if c not in ("ticker", "period_type", "raw", "filing_date")]
    yearly = yearly[cols].copy()
    yearly["date"] = pd.to_datetime(yearly["date"]).dt.date.astype(str)
    # Build a Plotly bar of top 5 magnitudes
    fig = None
    if statement == "income":
        m = yearly[["date", "total_revenue", "net_income"]].dropna(how="any")
        if not m.empty:
            fig = go.Figure()
            fig.add_bar(x=m["date"], y=m["total_revenue"].astype(float),
                        name="Total revenue")
            fig.add_bar(x=m["date"], y=m["net_income"].astype(float),
                        name="Net income")
            fig.update_layout(barmode="group", template="plotly_white",
                              margin=dict(l=20, r=20, t=20, b=20),
                              height=320)
    elif statement == "balance":
        m = yearly[["date", "total_assets", "total_liab",
                    "total_stockholder_equity"]].dropna(how="any")
        if not m.empty:
            fig = go.Figure()
            fig.add_bar(x=m["date"], y=m["total_assets"].astype(float), name="Assets")
            fig.add_bar(x=m["date"], y=m["total_liab"].astype(float), name="Liabilities")
            fig.add_bar(x=m["date"], y=m["total_stockholder_equity"].astype(float),
                        name="Equity")
            fig.update_layout(barmode="group", template="plotly_white",
                              margin=dict(l=20, r=20, t=20, b=20), height=320)
    elif statement == "cashflow":
        m = yearly[["date", "operating_cash_flow", "investing_cash_flow",
                    "financing_cash_flow", "free_cash_flow"]].dropna(how="all")
        if not m.empty:
            fig = go.Figure()
            for col, name in (("operating_cash_flow", "Operating"),
                              ("investing_cash_flow", "Investing"),
                              ("financing_cash_flow", "Financing"),
                              ("free_cash_flow", "Free cash flow")):
                if col in m:
                    fig.add_bar(x=m["date"], y=m[col].astype(float), name=name)
            fig.update_layout(barmode="group", template="plotly_white",
                              margin=dict(l=20, r=20, t=20, b=20), height=320)

    return html.Div([
        dcc.Graph(figure=fig) if fig is not None else None,
        html.H6("Annual"),
        _financials_table(yearly),
        html.H6("Quarterly", className="mt-3"),
        _financials_table(load_financials(ticker, statement, "quarterly")),
    ])


def _financials_table(df: pd.DataFrame) -> Any:
    if df.empty:
        return html.Div("No data.", className="text-muted")
    cols = [c for c in df.columns
            if c not in ("ticker", "period_type", "raw", "filing_date")]
    df = df[cols].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=[{"name": c, "id": c,
                  "type": "numeric" if c != "date" else "text",
                  "format": {"specifier": ",.0f"} if c != "date" else None}
                 for c in cols],
        style_cell={"padding": "4px", "fontSize": "12px"},
        style_table={"overflowX": "auto"},
    )


def _earnings_view(ticker: str, f: dict) -> html.Div:
    hist = fetch_all(
        "SELECT report_date,eps_actual,eps_estimate,surprise_pct "
        "FROM earnings_history WHERE ticker=%s "
        "ORDER BY report_date DESC LIMIT 24",
        (ticker,),
    )
    if not hist:
        return _json_table(f.get("earnings"))
    df = pd.DataFrame(hist)
    df["report_date"] = pd.to_datetime(df["report_date"]).dt.date.astype(str)
    fig = go.Figure()
    fig.add_bar(x=df["report_date"], y=df["eps_estimate"].astype(float),
                name="EPS estimate")
    fig.add_bar(x=df["report_date"], y=df["eps_actual"].astype(float),
                name="EPS actual")
    fig.update_layout(barmode="group", template="plotly_white",
                      margin=dict(l=20, r=20, t=20, b=20), height=320)
    return html.Div([
        dcc.Graph(figure=fig),
        dash_table.DataTable(
            data=df.to_dict("records"),
            columns=[{"name": c, "id": c} for c in df.columns],
            style_cell={"padding": "4px"},
        ),
    ])


def _divsplits_view(ticker: str) -> html.Div:
    divs = pd.DataFrame(fetch_all(
        "SELECT ex_date,value,currency FROM dividends "
        "WHERE ticker=%s ORDER BY ex_date DESC LIMIT 60", (ticker,)))
    splits = pd.DataFrame(fetch_all(
        "SELECT date,split_text FROM splits WHERE ticker=%s "
        "ORDER BY date DESC", (ticker,)))
    children = []
    if not divs.empty:
        divs["ex_date"] = pd.to_datetime(divs["ex_date"]).dt.date.astype(str)
        divs["value"] = divs["value"].astype(float)
        fig = go.Figure(go.Bar(x=divs["ex_date"], y=divs["value"], name="Dividend"))
        fig.update_layout(template="plotly_white", height=300,
                          margin=dict(l=20, r=20, t=20, b=20))
        children += [html.H6("Dividends"), dcc.Graph(figure=fig),
                     dash_table.DataTable(
                         data=divs.to_dict("records"),
                         columns=[{"name": c, "id": c} for c in divs.columns],
                         style_cell={"padding": "4px"}, page_size=20)]
    else:
        children.append(html.Div("No dividends.", className="text-muted"))

    if not splits.empty:
        splits["date"] = pd.to_datetime(splits["date"]).dt.date.astype(str)
        children += [html.H6("Splits", className="mt-3"),
                     dash_table.DataTable(
                         data=splits.to_dict("records"),
                         columns=[{"name": c, "id": c} for c in splits.columns],
                         style_cell={"padding": "4px"})]
    return html.Div(children)


def _ssg_cagr(first: float, last: float, periods: int) -> float | None:
    """Compound annual growth rate between two positive values."""
    if first is None or last is None or periods <= 0:
        return None
    if first <= 0 or last <= 0:
        return None
    return (last / first) ** (1.0 / periods) - 1.0


def _ssg_fit_loglinear(years: np.ndarray, values: np.ndarray):
    """Least-squares fit of log(value) ~ year. Returns (slope, intercept,
    annual_growth_rate) or None when there aren't enough positive points."""
    mask = np.isfinite(values) & (values > 0)
    if mask.sum() < 2:
        return None
    x = years[mask].astype(float)
    y = np.log(values[mask].astype(float))
    slope, intercept = np.polyfit(x, y, 1)
    growth = float(np.exp(slope) - 1.0)
    return float(slope), float(intercept), growth


def build_ssg(ticker: str, f: dict | None) -> html.Div:
    """NAIC Stock Selection Guide.

    Section 1  - semi-log history of Sales, EPS and Price with growth trendlines.
    Section 3  - P/E history (approx. 5yr high/low) and payout ratio.
    Section 4  - projected 5-year high/low price and buy / maybe / sell zones.

    Degrades gracefully: with thin history it shows what it can and explains
    what's missing rather than erroring.
    """
    hist = load_ssg_history(ticker, years=10)
    prices = load_prices(ticker, days=365 * 11)

    if hist.empty and prices.empty:
        return html.Div(
            "Not enough stored data to build an SSG. Ingest fundamentals and "
            "EOD prices for this ticker first.",
            className="alert alert-warning")

    notes: list[str] = []
    f = f or {}

    def _num(key):
        v = f.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    cur_eps = _num("eps")
    cur_pe = _num("pe_ratio")
    div_share = _num("dividend_share")
    last_price = float(prices["close"].iloc[-1]) if not prices.empty else None

    # ---- Section 1: semi-log growth chart ----------------------------
    sec1 = go.Figure()
    have_growth: dict[str, float] = {}
    if not hist.empty:
        yrs = hist["year"].to_numpy()
        for col, name, color in (("sales", "Sales", "#1f77b4"),
                                 ("eps", "EPS", "#2ca02c")):
            vals = pd.to_numeric(hist[col], errors="coerce").to_numpy()
            if np.isfinite(vals).sum() == 0:
                continue
            sec1.add_trace(go.Scatter(
                x=hist["year"], y=vals, mode="lines+markers", name=name,
                line={"color": color}))
            fit = _ssg_fit_loglinear(yrs, vals)
            if fit:
                slope, intercept, growth = fit
                have_growth[col] = growth
                xs = np.array([yrs.min(), yrs.max()], dtype=float)
                ys = np.exp(intercept + slope * xs)
                sec1.add_trace(go.Scatter(
                    x=xs, y=ys, mode="lines", name=f"{name} trend",
                    line={"color": color, "dash": "dash", "width": 1}))
        if not prices.empty:
            pr = prices.copy()
            pr["year"] = pr["date"].dt.year
            ye = pr.groupby("year")["close"].last()
            sec1.add_trace(go.Scatter(
                x=ye.index, y=ye.values, mode="lines+markers",
                name="Price (yr-end)", line={"color": "#d62728"}))
        sec1.update_yaxes(type="log", title="Log scale")
        sec1.update_layout(
            title="Section 1 — Sales, EPS & Price (semi-log)",
            height=420, margin={"t": 48, "b": 30},
            legend={"orientation": "h", "y": -0.18})
    else:
        notes.append("No annual sales/EPS history stored — Section 1 growth "
                     "trendlines unavailable.")

    sales_growth = have_growth.get("sales")
    eps_growth = have_growth.get("eps")

    # ---- Section 3: P/E history --------------------------------------
    pe_high = pe_low = None
    if not prices.empty and cur_eps and cur_eps > 0:
        pr = prices.copy()
        recent = pr[pr["date"] >= (pd.Timestamp.today() - pd.DateOffset(years=5))]
        if not recent.empty:
            pe_high = float(recent["high"].max() / cur_eps)
            pe_low = float(recent["low"].min() / cur_eps)

    payout = None
    if div_share is not None and cur_eps and cur_eps > 0:
        payout = div_share / cur_eps

    # ---- Section 4: projected price zones ----------------------------
    zone: dict = {}
    proj_eps5 = None
    if cur_eps and cur_eps > 0 and eps_growth is not None:
        proj_eps5 = cur_eps * ((1 + eps_growth) ** 5)
    if proj_eps5 and pe_high and pe_low:
        hi = proj_eps5 * pe_high
        lo = cur_eps * pe_low
        if hi > lo:
            rng = hi - lo
            buy_top = lo + rng / 3.0
            sell_bottom = hi - rng / 3.0
            zone = {
                "forecast_high": hi,
                "forecast_low": lo,
                "buy_below": buy_top,
                "maybe_between": (buy_top, sell_bottom),
                "sell_above": sell_bottom,
            }

    def _pct(v):
        return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "—"

    def _money(v):
        return f"{v:,.2f}" if isinstance(v, (int, float)) else "—"

    stat_rows = [
        ("Historical sales growth (trend)", _pct(sales_growth)),
        ("Historical EPS growth (trend)", _pct(eps_growth)),
        ("Current trailing EPS", _money(cur_eps)),
        ("Current P/E", _money(cur_pe)),
        ("5-yr high P/E (approx.)", _money(pe_high)),
        ("5-yr low P/E (approx.)", _money(pe_low)),
        ("Payout ratio", _pct(payout)),
        ("Projected EPS (5 yr)", _money(proj_eps5)),
    ]
    stat_table = dash_table.DataTable(
        data=[{"metric": m, "value": v} for m, v in stat_rows],
        columns=[{"name": "Metric", "id": "metric"},
                 {"name": "Value", "id": "value"}],
        style_cell={"padding": "4px", "textAlign": "left"},
        style_table={"maxWidth": "460px"})

    if zone:
        cur_txt = f" (current price {_money(last_price)})" if last_price else ""
        zone_block = html.Div([
            html.H6("Section 4 — Five-year price zones"),
            html.Ul([
                html.Li(f"Forecast high price: {_money(zone['forecast_high'])}"),
                html.Li(f"Forecast low price: {_money(zone['forecast_low'])}"),
                html.Li([html.Span("BUY", className="badge bg-success me-1"),
                         f"below {_money(zone['buy_below'])}"]),
                html.Li([html.Span("MAYBE", className="badge bg-warning text-dark me-1"),
                         f"{_money(zone['maybe_between'][0])} – "
                         f"{_money(zone['maybe_between'][1])}"]),
                html.Li([html.Span("SELL", className="badge bg-danger me-1"),
                         f"above {_money(zone['sell_above'])}"]),
            ]),
            html.P(f"Zones split the forecast high–low range into thirds "
                   f"(NAIC convention).{cur_txt}",
                   className="text-muted small"),
        ])
    else:
        zone_block = html.Div(
            "Section 4 price zones need a current EPS, an EPS growth rate, and "
            "a P/E range — one or more is missing for this ticker.",
            className="alert alert-secondary")
        notes.append("Projected price zones unavailable (insufficient EPS / "
                     "P/E history).")

    children = [
        html.H5("NAIC Stock Selection Guide"),
        html.P("A judgment aid, not advice. Growth rates are fit from stored "
               "history and may be short for thinly-covered tickers.",
               className="text-muted small"),
    ]
    if not hist.empty:
        children.append(dcc.Graph(figure=sec1, config={"displayModeBar": False}))
    children.append(html.Div([
        html.Div([html.H6("Section 3 — Evaluating risk & reward"), stat_table],
                 className="col-md-6"),
        html.Div(zone_block, className="col-md-6"),
    ], className="row mt-3"))
    if notes:
        children.append(html.Ul([html.Li(n) for n in notes],
                                className="text-muted small mt-2"))
    return html.Div(children, className="border rounded p-3")


def _holders_view(ticker: str) -> html.Div:
    inst = pd.DataFrame(fetch_all(
        "SELECT holder_name,report_date,shares_held,pct_shares,pct_assets "
        "FROM institutional_holders WHERE ticker=%s "
        "ORDER BY pct_shares DESC NULLS LAST LIMIT 50", (ticker,)))
    funds = pd.DataFrame(fetch_all(
        "SELECT holder_name,report_date,shares_held,pct_shares "
        "FROM fund_holders WHERE ticker=%s "
        "ORDER BY pct_shares DESC NULLS LAST LIMIT 50", (ticker,)))

    def _fmt(df: pd.DataFrame, has_assets: bool) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        if "report_date" in df:
            df["report_date"] = pd.to_datetime(
                df["report_date"], errors="coerce").dt.date.astype("string")
        # shares_held is a raw count -> thousands separators
        df["shares_held"] = pd.to_numeric(df["shares_held"], errors="coerce")
        df["shares_held"] = df["shares_held"].map(
            lambda v: f"{v:,.0f}" if pd.notna(v) else "")
        # pct_* are already percentages from EODHD (e.g. 1.25 == 1.25%)
        for c in (["pct_shares", "pct_assets"] if has_assets else ["pct_shares"]):
            df[c] = pd.to_numeric(df[c], errors="coerce")
            df[c] = df[c].map(lambda v: f"{v:.2f}%" if pd.notna(v) else "")
        return df

    inst = _fmt(inst, has_assets=True)
    funds = _fmt(funds, has_assets=False)

    labels = {
        "holder_name": "Holder",
        "report_date": "Report date",
        "shares_held": "Shares held",
        "pct_shares": "% of shares",
        "pct_assets": "% of holder assets",
    }

    def _table(df: pd.DataFrame) -> dash_table.DataTable:
        cols = list(df.columns) if not df.empty else []
        return dash_table.DataTable(
            data=df.to_dict("records") if not df.empty else [],
            columns=[{"name": labels.get(c, c), "id": c} for c in cols],
            page_size=15, style_cell={"padding": "4px"},
            style_cell_conditional=[
                {"if": {"column_id": c}, "textAlign": "right"}
                for c in ("shares_held", "pct_shares", "pct_assets")
            ],
            style_table={"overflowX": "auto"},
        )

    return html.Div([
        html.H6("Institutional holders"),
        _table(inst) if not inst.empty
        else html.Div("No institutional holders on file.", className="text-muted"),
        html.H6("Fund holders", className="mt-3"),
        _table(funds) if not funds.empty
        else html.Div("No fund holders on file.", className="text-muted"),
    ])


def _insider_view(ticker: str) -> html.Div:
    df = pd.DataFrame(fetch_all(
        "SELECT transaction_date,owner_name,relationship,transaction_code,"
        "acquisition_or_disposition,shares,price,value "
        "FROM insider_transactions WHERE ticker=%s "
        "ORDER BY transaction_date DESC LIMIT 100", (ticker,)))
    if df.empty:
        return html.Div("No insider transactions on file.", className="text-muted")
    df["transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.date.astype(str)
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=[{"name": c, "id": c} for c in df.columns],
        page_size=25, style_cell={"padding": "4px"},
        style_table={"overflowX": "auto"},
    )


def _news_view(ticker: str) -> html.Div:
    df = load_news(ticker, limit=50)
    if df.empty:
        return html.Div("No news.", className="text-muted")
    items = []
    for _, r in df.iterrows():
        # Pull values out as scalars defensively — never use Series in booleans.
        s_raw = r["sentiment_polarity"] if "sentiment_polarity" in r.index else None
        try:
            s = float(s_raw) if s_raw is not None and pd.notna(s_raw) else None
        except (TypeError, ValueError):
            s = None
        color = "secondary"
        if s is not None:
            color = "success" if s > 0.05 else ("danger" if s < -0.05 else "secondary")
        published = r["published_at"] if "published_at" in r.index else None
        try:
            published_str = pd.to_datetime(published).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            published_str = str(published or "")
        title = str(r["title"]) if "title" in r.index and pd.notna(r["title"]) else "(untitled)"
        link = str(r["link"]) if "link" in r.index and pd.notna(r["link"]) else "#"
        items.append(html.Li([
            html.A(title, href=link, target="_blank"),
            html.Span(f" {published_str}", className="text-muted small ms-2"),
            html.Span(f" sentiment={s:+.2f}" if s is not None else "",
                      className=f"badge bg-{color} ms-2"),
        ], className="my-1"))
    return html.Ul(items, className="list-unstyled")


def _json_table(payload: Any) -> html.Div:
    # Defensive against being handed a DataFrame (truth-value error) or a list.
    if isinstance(payload, pd.DataFrame):
        if payload.empty:
            return html.Div("No data.", className="text-muted")
        return dash_table.DataTable(
            data=payload.astype(str).to_dict("records"),
            columns=[{"name": c, "id": c} for c in payload.columns],
            style_cell={"padding": "4px", "whiteSpace": "normal",
                        "height": "auto", "textAlign": "left"},
            style_table={"maxWidth": "900px", "overflowX": "auto"},
        )
    if payload is None or payload == "" or payload == [] or payload == {}:
        return html.Div("No data.", className="text-muted")
    if isinstance(payload, dict):
        rows = [{"Key": k, "Value": str(v)[:200]} for k, v in payload.items()]
        return dash_table.DataTable(
            data=rows,
            columns=[{"name": c, "id": c} for c in ("Key", "Value")],
            style_cell={"padding": "4px", "whiteSpace": "normal",
                        "height": "auto", "textAlign": "left"},
            style_table={"maxWidth": "900px"},
        )
    if isinstance(payload, list):
        if not payload:
            return html.Div("No data.", className="text-muted")
        if isinstance(payload[0], dict):
            cols = list({k for d in payload for k in d.keys()})
            return dash_table.DataTable(
                data=[{c: str(d.get(c, ""))[:200] for c in cols} for d in payload],
                columns=[{"name": c, "id": c} for c in cols],
                style_cell={"padding": "4px", "whiteSpace": "normal",
                            "height": "auto", "textAlign": "left"},
                style_table={"maxWidth": "900px", "overflowX": "auto"},
                page_size=20,
            )
    return html.Pre(str(payload))


# ---------------------------------------------------------------------
# Portfolio CRUD callbacks
# ---------------------------------------------------------------------
def _portfolio_form(initial: dict | None = None) -> html.Div:
    initial = initial or {}
    return html.Div([
        html.Div([
            html.Label("Name"),
            dcc.Input(id="pf-name", value=initial.get("name", ""),
                      className="form-control"),
        ], className="mb-2"),
        html.Div([
            html.Label("Description"),
            dcc.Input(id="pf-description", value=initial.get("description", "") or "",
                      className="form-control"),
        ], className="mb-2"),
        html.Div([
            html.Div([
                html.Label("Base currency"),
                dcc.Input(id="pf-currency",
                          value=initial.get("base_currency", "USD"),
                          className="form-control"),
            ], className="col-md-3"),
            html.Div([
                html.Label("Initial cash"),
                dcc.Input(id="pf-cash", type="number", min=0,
                          value=float(initial.get("initial_cash") or 0),
                          className="form-control"),
            ], className="col-md-3"),
        ], className="row mb-2"),
        html.Button(
            "Save" if initial else "Create",
            id="pf-save-btn",
            className="btn btn-primary me-2",
        ),
        html.Button("Cancel", id="pf-cancel-btn", className="btn btn-secondary"),
        dcc.Store(id="pf-edit-id", data=_serialise(initial.get("id"))),
    ], className="border rounded p-3 bg-light")


@app.callback(
    Output("portfolio-form-area", "children"),
    Input("new-portfolio-btn", "n_clicks"),
    Input("portfolios-table", "active_cell"),
    State("portfolios-table", "data"),
    prevent_initial_call=True,
)
def open_portfolio_form(_n, active_cell, data):
    trig = callback_context.triggered_id
    if trig == "new-portfolio-btn":
        return _portfolio_form()
    if trig == "portfolios-table" and active_cell:
        col = active_cell.get("column_id")
        row = data[active_cell["row"]]
        if col == "edit_link":
            initial = pf.get_portfolio(row["id"])
            return _portfolio_form(initial) if initial else no_update
        if col == "delete_link":
            pf.delete_portfolio(row["id"])
            # Refresh page so the table updates.
            return html.Div([
                html.Div("✓ Portfolio deleted. Refresh to see updated list.",
                         className="alert alert-success"),
                dcc.Location(id="reload-portfolios", href="/portfolios"),
            ])
    return no_update


@app.callback(
    Output("portfolio-toast", "children"),
    Output("portfolio-form-area", "children", allow_duplicate=True),
    Input("pf-save-btn", "n_clicks"),
    Input("pf-cancel-btn", "n_clicks"),
    State("pf-name", "value"),
    State("pf-description", "value"),
    State("pf-currency", "value"),
    State("pf-cash", "value"),
    State("pf-edit-id", "data"),
    prevent_initial_call=True,
)
def save_portfolio(_s, _c, name, description, currency, cash, edit_id):
    trig = callback_context.triggered_id
    if trig == "pf-cancel-btn":
        return None, None
    if not name or not name.strip():
        return html.Div("Name is required.", className="alert alert-danger"), no_update
    try:
        if edit_id:
            pf.update_portfolio(edit_id, name=name, description=description or "",
                                base_currency=currency or "USD",
                                initial_cash=cash or 0)
            msg = "✓ Portfolio updated."
        else:
            pf.create_portfolio(name=name, description=description or "",
                                base_currency=currency or "USD",
                                initial_cash=cash or 0)
            msg = "✓ Portfolio created."
        return (
            html.Div([msg, " ", dcc.Link("Reload list", href="/portfolios")],
                     className="alert alert-success"),
            None,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("portfolio save")
        return html.Div(f"✗ {e}", className="alert alert-danger"), no_update


# ---------------------------------------------------------------------
# Trade CRUD callbacks
# ---------------------------------------------------------------------
def _trade_form(portfolio_id: str, initial: dict | None = None) -> html.Div:
    initial = initial or {}
    today = date.today().isoformat()
    return html.Div([
        html.Div([
            html.Div([
                html.Label("Ticker"),
                dcc.Input(id="tr-ticker",
                          value=initial.get("ticker", ""),
                          className="form-control", placeholder="AAPL.US"),
            ], className="col-md-3"),
            html.Div([
                html.Label("Side"),
                dcc.Dropdown(
                    id="tr-side",
                    options=[{"label": "BUY", "value": "BUY"},
                             {"label": "SELL", "value": "SELL"}],
                    value=initial.get("side", "BUY"),
                    clearable=False,
                ),
            ], className="col-md-2"),
            html.Div([
                html.Label("Date"),
                dcc.Input(id="tr-date", type="text",
                          value=_serialise(initial.get("trade_date")) or today,
                          className="form-control"),
            ], className="col-md-2"),
        ], className="row mb-2"),
        html.Div([
            html.Div([
                html.Label("Quantity"),
                dcc.Input(id="tr-qty", type="number", min=0,
                          value=float(initial.get("quantity") or 0),
                          className="form-control"),
            ], className="col-md-2"),
            html.Div([
                html.Label("Price"),
                dcc.Input(id="tr-price", type="number", min=0,
                          value=float(initial.get("price") or 0),
                          className="form-control"),
            ], className="col-md-2"),
            html.Div([
                html.Label("Fees"),
                dcc.Input(id="tr-fees", type="number", min=0,
                          value=float(initial.get("fees") or 0),
                          className="form-control"),
            ], className="col-md-2"),
            html.Div([
                html.Label("Currency"),
                dcc.Input(id="tr-currency",
                          value=initial.get("currency", "USD"),
                          className="form-control"),
            ], className="col-md-2"),
        ], className="row mb-2"),
        html.Div([
            html.Label("Notes"),
            dcc.Input(id="tr-notes",
                      value=initial.get("notes", "") or "",
                      className="form-control"),
        ], className="mb-2"),
        html.Button("Save" if initial else "Add",
                    id="tr-save-btn", className="btn btn-primary me-2"),
        html.Button("Cancel", id="tr-cancel-btn", className="btn btn-secondary"),
        dcc.Store(id="tr-edit-id", data=_serialise(initial.get("id"))),
        dcc.Store(id="tr-portfolio-id", data=_serialise(portfolio_id)),
    ], className="border rounded p-3 bg-light")


@app.callback(
    Output("trade-form-area", "children"),
    Input("new-trade-btn", "n_clicks"),
    Input("trades-table", "active_cell"),
    State("trades-table", "data"),
    State("pid", "data"),
    prevent_initial_call=True,
)
def open_trade_form(_n, active_cell, data, portfolio_id):
    trig = callback_context.triggered_id
    if trig == "new-trade-btn":
        return _trade_form(portfolio_id)
    if trig == "trades-table" and active_cell:
        col = active_cell.get("column_id")
        row = data[active_cell["row"]]
        if col == "edit_link":
            existing = pf.get_trade(row["id"])
            if existing:
                # Ensure trade_date is a date for the input.
                if existing.get("trade_date") and not isinstance(
                        existing["trade_date"], str):
                    existing["trade_date"] = existing["trade_date"].isoformat()
                return _trade_form(portfolio_id, existing)
        if col == "delete_link":
            pf.delete_trade(row["id"])
            return html.Div([
                html.Div("✓ Trade deleted.", className="alert alert-success"),
                dcc.Location(id="reload-portfolio", href=f"/portfolios/{portfolio_id}"),
            ])
    return no_update


@app.callback(
    Output("trade-toast", "children"),
    Output("trade-form-area", "children", allow_duplicate=True),
    Input("tr-save-btn", "n_clicks"),
    Input("tr-cancel-btn", "n_clicks"),
    State("tr-ticker", "value"),
    State("tr-side", "value"),
    State("tr-date", "value"),
    State("tr-qty", "value"),
    State("tr-price", "value"),
    State("tr-fees", "value"),
    State("tr-currency", "value"),
    State("tr-notes", "value"),
    State("tr-edit-id", "data"),
    State("tr-portfolio-id", "data"),
    prevent_initial_call=True,
)
def save_trade(_s, _c, ticker, side, trade_date, qty, price, fees,
               currency, notes, edit_id, portfolio_id):
    trig = callback_context.triggered_id
    if trig == "tr-cancel-btn":
        return None, None
    if not ticker or not qty or qty <= 0 or price is None or price < 0:
        return html.Div("Ticker, positive quantity and non-negative price are required.",
                        className="alert alert-danger"), no_update
    try:
        td = datetime.strptime(trade_date, "%Y-%m-%d").date() \
            if isinstance(trade_date, str) else trade_date
    except ValueError:
        return html.Div("Invalid date — use YYYY-MM-DD.",
                        className="alert alert-danger"), no_update
    try:
        if edit_id:
            pf.update_trade(edit_id, ticker=ticker.strip().upper(),
                            side=side, trade_date=td, quantity=qty,
                            price=price, fees=fees or 0,
                            currency=currency or "USD", notes=notes or "")
            msg = "✓ Trade updated."
        else:
            pf.create_trade(portfolio_id, ticker=ticker.strip().upper(),
                            side=side, trade_date=td, quantity=qty,
                            price=price, fees=fees or 0,
                            currency=currency or "USD", notes=notes or "")
            msg = "✓ Trade added."
        return (
            html.Div([msg, " ",
                      dcc.Link("Reload", href=f"/portfolios/{portfolio_id}")],
                     className="alert alert-success"),
            None,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("trade save")
        return html.Div(f"✗ {e}", className="alert alert-danger"), no_update


def _equity_curve_fig(portfolio_id: str) -> go.Figure:
    rows = pf.portfolio_value_history(portfolio_id)
    if not rows:
        fig = go.Figure()
        fig.add_annotation(text="No price data yet — ingest EOD for held tickers.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        return fig
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["market_value"] = df["market_value"].astype(float)
    fig = go.Figure(go.Scatter(x=df["date"], y=df["market_value"],
                               mode="lines", fill="tozeroy", name="Mkt value"))
    fig.update_layout(template="plotly_white",
                      margin=dict(l=20, r=20, t=20, b=20), height=320)
    return fig


# ---------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host=settings.dash_host, port=settings.dash_port,
            debug=settings.dash_debug)
