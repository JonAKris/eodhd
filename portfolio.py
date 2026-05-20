"""
portfolio.py
------------
CRUD layer for portfolios and trades. Pure DB code — no UI dependency.

The Dash callbacks import these functions; you can also use them
from a notebook or other tooling.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from db import execute, fetch_all, fetch_one

DEMO_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


# =====================================================================
# Portfolios
# =====================================================================
def list_portfolios(user_id: UUID = DEMO_USER_ID) -> list[dict]:
    return fetch_all(
        """SELECT p.*, ps.trade_count, ps.ticker_count,
                  ps.gross_invested, ps.gross_proceeds
             FROM portfolios p
             LEFT JOIN portfolio_summary ps ON ps.portfolio_id = p.id
            WHERE p.user_id = %s
         ORDER BY p.name""",
        (user_id,),
    )


def get_portfolio(portfolio_id: UUID | str) -> dict | None:
    return fetch_one(
        """SELECT p.*, ps.trade_count, ps.ticker_count,
                  ps.gross_invested, ps.gross_proceeds
             FROM portfolios p
             LEFT JOIN portfolio_summary ps ON ps.portfolio_id = p.id
            WHERE p.id = %s""",
        (portfolio_id,),
    )


def create_portfolio(name: str, description: str = "",
                     base_currency: str = "USD",
                     initial_cash: float | Decimal = 0,
                     user_id: UUID = DEMO_USER_ID) -> dict:
    row = fetch_one(
        """INSERT INTO portfolios (user_id,name,description,base_currency,initial_cash)
           VALUES (%s,%s,%s,%s,%s)
           RETURNING *""",
        (user_id, name.strip(), description.strip(), base_currency, initial_cash),
    )
    return row


def update_portfolio(portfolio_id: UUID | str, *,
                     name: str | None = None,
                     description: str | None = None,
                     base_currency: str | None = None,
                     initial_cash: float | Decimal | None = None) -> dict | None:
    sets: list[str] = []
    vals: list[Any] = []
    if name is not None:
        sets.append("name=%s"); vals.append(name.strip())
    if description is not None:
        sets.append("description=%s"); vals.append(description.strip())
    if base_currency is not None:
        sets.append("base_currency=%s"); vals.append(base_currency)
    if initial_cash is not None:
        sets.append("initial_cash=%s"); vals.append(initial_cash)
    if not sets:
        return get_portfolio(portfolio_id)
    vals.append(portfolio_id)
    sql = f"UPDATE portfolios SET {', '.join(sets)} WHERE id=%s RETURNING *"
    return fetch_one(sql, tuple(vals))


def delete_portfolio(portfolio_id: UUID | str) -> int:
    """Deleting a portfolio cascades to its trades."""
    return execute("DELETE FROM portfolios WHERE id=%s", (portfolio_id,))


# =====================================================================
# Trades
# =====================================================================
def list_trades(portfolio_id: UUID | str) -> list[dict]:
    return fetch_all(
        """SELECT t.*, s.name AS symbol_name
             FROM trades t
        LEFT JOIN symbols s ON s.ticker = t.ticker
            WHERE t.portfolio_id = %s
         ORDER BY t.trade_date DESC, t.created_at DESC""",
        (portfolio_id,),
    )


def get_trade(trade_id: UUID | str) -> dict | None:
    return fetch_one("SELECT * FROM trades WHERE id=%s", (trade_id,))


def create_trade(portfolio_id: UUID | str, ticker: str, side: str,
                 trade_date: date, quantity: float | Decimal,
                 price: float | Decimal, fees: float | Decimal = 0,
                 currency: str = "USD", notes: str = "") -> dict:
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    # Make sure the symbol exists so the FK is satisfied even for
    # tickers we haven't ingested fundamentals for yet.
    code, _, exch = ticker.partition(".")
    if not exch:
        raise ValueError(f"ticker {ticker!r} must look like CODE.EXCHANGE")
    execute(
        "INSERT INTO exchanges (code,name) VALUES (%s,%s) "
        "ON CONFLICT (code) DO NOTHING",
        (exch, exch),
    )
    execute(
        "INSERT INTO symbols (ticker,code,exchange_code,is_active) "
        "VALUES (%s,%s,%s,TRUE) ON CONFLICT (ticker) DO NOTHING",
        (ticker, code, exch),
    )
    return fetch_one(
        """INSERT INTO trades (portfolio_id,ticker,side,trade_date,quantity,
                               price,fees,currency,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING *""",
        (portfolio_id, ticker, side, trade_date, quantity, price, fees, currency, notes),
    )


def update_trade(trade_id: UUID | str, **fields: Any) -> dict | None:
    allowed = {"ticker", "side", "trade_date", "quantity", "price",
               "fees", "currency", "notes"}
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "side":
            v = str(v).upper()
            if v not in ("BUY", "SELL"):
                raise ValueError("side must be BUY or SELL")
        sets.append(f"{k}=%s"); vals.append(v)
    if not sets:
        return get_trade(trade_id)
    vals.append(trade_id)
    return fetch_one(
        f"UPDATE trades SET {', '.join(sets)} WHERE id=%s RETURNING *",
        tuple(vals),
    )


def delete_trade(trade_id: UUID | str) -> int:
    return execute("DELETE FROM trades WHERE id=%s", (trade_id,))


# =====================================================================
# Positions & P&L
# =====================================================================
def positions(portfolio_id: UUID | str) -> list[dict]:
    """
    Current open positions with cost basis, latest close, market value
    and unrealised P&L pulled from eod_prices.
    """
    return fetch_all(
        """
        WITH pos AS (
            SELECT * FROM portfolio_positions WHERE portfolio_id = %s
        ),
        last_px AS (
            SELECT DISTINCT ON (ticker) ticker, date AS as_of, adjusted_close, close
              FROM eod_prices
             WHERE ticker IN (SELECT ticker FROM pos)
          ORDER BY ticker, date DESC
        )
        SELECT
            p.portfolio_id, p.ticker,
            s.name AS symbol_name, f.sector, f.industry, f.currency,
            p.quantity, p.avg_buy_price, p.cost_basis,
            COALESCE(lp.adjusted_close, lp.close) AS last_price,
            lp.as_of                                AS price_as_of,
            (COALESCE(lp.adjusted_close, lp.close) * p.quantity) AS market_value,
            ((COALESCE(lp.adjusted_close, lp.close) * p.quantity) - p.cost_basis)
                AS unrealised_pnl,
            CASE
              WHEN p.cost_basis = 0 THEN NULL
              ELSE ((COALESCE(lp.adjusted_close, lp.close) * p.quantity) - p.cost_basis)
                   / NULLIF(p.cost_basis, 0) * 100
            END AS unrealised_pnl_pct
        FROM pos p
        LEFT JOIN symbols      s ON s.ticker = p.ticker
        LEFT JOIN fundamentals f ON f.ticker = p.ticker
        LEFT JOIN last_px      lp ON lp.ticker = p.ticker
        ORDER BY p.ticker
        """,
        (portfolio_id,),
    )


def portfolio_value_history(portfolio_id: UUID | str) -> list[dict]:
    """
    Daily marked-to-market portfolio value series.
    Walks trades into a running position and joins to eod_prices.
    """
    return fetch_all(
        """
        WITH dates AS (
            SELECT DISTINCT date
              FROM eod_prices
             WHERE ticker IN (SELECT ticker FROM trades WHERE portfolio_id = %(pid)s)
        ),
        position_on_date AS (
            SELECT d.date,
                   t.ticker,
                   SUM(CASE WHEN t.side='BUY' THEN t.quantity ELSE -t.quantity END) AS qty
              FROM dates d
              JOIN trades t ON t.portfolio_id = %(pid)s AND t.trade_date <= d.date
          GROUP BY d.date, t.ticker
        ),
        valued AS (
            SELECT p.date,
                   SUM(p.qty * COALESCE(e.adjusted_close, e.close)) AS market_value
              FROM position_on_date p
              JOIN eod_prices e ON e.ticker = p.ticker AND e.date = p.date
          GROUP BY p.date
        )
        SELECT date, market_value
          FROM valued
      ORDER BY date
        """,
        {"pid": portfolio_id},
    )
