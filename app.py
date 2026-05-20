
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
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import dash
import pandas as pd
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
        html.Div([
            dcc.Input(id="ingest-ticker", placeholder="AAPL.US", type="text",
                      className="form-control d-inline-block w-25 me-2"),
            html.Button("Ingest everything for ticker", id="ingest-btn",
                        className="btn btn-primary"),
            html.Span(id="ingest-status", className="ms-3"),
        ], className="d-flex align-items-center"),
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
        ], className="d-flex align-items-center flex-wrap mb-3"),

        dcc.Loading(dcc.Graph(id="price-chart", style={"height": "620px"})),

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
        dcc.Store(id="pid", data=portfolio_id),
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
    Output("ingest-status", "children"),
    Input("ingest-btn", "n_clicks"),
    State("ingest-ticker", "value"),
    prevent_initial_call=True,
)
def trigger_ingest(_clicks: int, ticker: str):
    if not ticker:
        return "Enter a ticker first."
    try:
        ingestor.ingest_all_for_ticker(ticker.strip().upper())
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
            fig.add_trace(go.Bar(x=df["date"], y=df["volume"], name="Volume",
                                 marker=dict(color="rgba(120,120,120,0.5)")),
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


def _holders_view(ticker: str) -> html.Div:
    inst = pd.DataFrame(fetch_all(
        "SELECT holder_name,report_date,total_shares,total_assets,pct_held "
        "FROM institutional_holders WHERE ticker=%s "
        "ORDER BY total_shares DESC NULLS LAST LIMIT 50", (ticker,)))
    funds = pd.DataFrame(fetch_all(
        "SELECT holder_name,report_date,total_shares,pct_held "
        "FROM fund_holders WHERE ticker=%s "
        "ORDER BY total_shares DESC NULLS LAST LIMIT 50", (ticker,)))
    return html.Div([
        html.H6("Institutional holders"),
        dash_table.DataTable(
            data=inst.to_dict("records") if not inst.empty else [],
            columns=[{"name": c, "id": c} for c in (inst.columns if not inst.empty else [])],
            page_size=15, style_cell={"padding": "4px"},
            style_table={"overflowX": "auto"},
        ),
        html.H6("Fund holders", className="mt-3"),
        dash_table.DataTable(
            data=funds.to_dict("records") if not funds.empty else [],
            columns=[{"name": c, "id": c} for c in (funds.columns if not funds.empty else [])],
            page_size=15, style_cell={"padding": "4px"},
            style_table={"overflowX": "auto"},
        ),
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
        dcc.Store(id="pf-edit-id", data=initial.get("id")),
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
                          value=initial.get("trade_date") or today,
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
        dcc.Store(id="tr-edit-id", data=initial.get("id")),
        dcc.Store(id="tr-portfolio-id", data=portfolio_id),
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
