"""
ingest.py
---------
Pulls data from EODHD using the official ``eodhd`` PyPI library
(``pip install eodhd``) and loads it into Postgres.

CLI examples:

    # one-time: pull every exchange & each exchange's symbol list
    python ingest.py exchanges
    python ingest.py symbols US

    # for a single ticker, pull *everything*
    python ingest.py all AAPL.US

    # daily refresh of EOD prices for everything in the symbols table
    python ingest.py eod-refresh --since 2024-01-01

    # one-shot news pull
    python ingest.py news AAPL.US MSFT.US NVDA.US
"""

from __future__ import annotations

import argparse
import logging
import math
from datetime import date, datetime
import datetime as dt
from typing import Any

import pandas as pd
import requests

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy ships with pandas
    np = None  # type: ignore

from eodhd import APIClient
from psycopg.types.json import Json

from config import settings
from db import connection, execute, execute_many, fetch_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("ingest")


# ---------------------------------------------------------------------
# Response normalisers
# ---------------------------------------------------------------------
# The `eodhd` library is inconsistent: some endpoints return a pandas
# DataFrame, others return a list[dict], others return a dict. These
# helpers always give us the type we want — never use `value or []`
# on an API result directly because `bool(DataFrame)` raises.
def _as_list(resp: Any) -> list[dict]:
    """Coerce an eodhd response into a list[dict]."""
    if resp is None:
        return []
    if isinstance(resp, pd.DataFrame):
        if resp.empty:
            return []
        # If the index is a real DatetimeIndex (time-series endpoints
        # like get_eod_historical_stock_market_data return one), reset
        # it into a column. Otherwise drop the RangeIndex.
        if isinstance(resp.index, pd.DatetimeIndex):
            df = resp.reset_index()
            if "index" in df.columns and "date" not in df.columns:
                df = df.rename(columns={"index": "date"})
        elif resp.index.name:
            df = resp.reset_index()
        else:
            df = resp.reset_index(drop=True)
        # Stringify any Timestamps so downstream _to_date works uniformly.
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
        records = df.to_dict("records")
        # Per-cell NaN/NaT replacement (pd.DataFrame.where can leak NaN on
        # object columns - this is the only fully reliable approach).
        return [_scrub_nans(r) for r in records]
    if isinstance(resp, pd.Series):
        if resp.empty:
            return []
        return [_scrub_nans(resp.to_dict())]
    if isinstance(resp, dict):
        # Sometimes eodhd wraps the list under a key like "data" or returns
        # a {date: row} mapping. Heuristic: if all values are dicts, treat
        # the keys as IDs and merge them in; otherwise wrap the dict itself.
        if resp and all(isinstance(v, dict) for v in resp.values()):
            out = []
            for k, v in resp.items():
                row = {**v}
                row.setdefault("_key", k)
                out.append(_scrub_nans(row))
            return out
        return [_scrub_nans(resp)]
    if isinstance(resp, list):
        return [_scrub_nans(r) for r in resp if isinstance(r, dict)]
    return []


def _scrub_nans(d: dict) -> dict:
    """Replace top-level NaN / NaT scalars in a dict with None."""
    out = {}
    for k, v in d.items():
        if v is None:
            out[k] = None
            continue
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            out[k] = None
            continue
        # numpy scalars / pandas Timestamps - try .item() / notna
        try:
            if pd.isna(v):
                out[k] = None
                continue
        except (TypeError, ValueError):
            pass
        out[k] = v
    return out


def _as_dict(resp: Any) -> dict:
    """Coerce an eodhd response into a dict (top-level object endpoints)."""
    if resp is None:
        return {}
    if isinstance(resp, pd.DataFrame):
        if resp.empty:
            return {}
        return resp.where(pd.notna(resp), None).iloc[0].to_dict()
    if isinstance(resp, pd.Series):
        if resp.empty:
            return {}
        return resp.where(pd.notna(resp), None).to_dict()
    if isinstance(resp, dict):
        return resp
    return {}


# ---------------------------------------------------------------------
# JSON cleaning
# ---------------------------------------------------------------------
# Postgres' JSON types reject NaN, Infinity, -Infinity. Pandas / numpy
# scatter these throughout nested structures returned by EODHD, so we
# walk the whole tree and replace them with None before persisting.
def _clean_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_json(v) for v in value]
    if np is not None:
        if isinstance(value, np.generic):
            return _clean_json(value.item())
        if isinstance(value, np.ndarray):
            return [_clean_json(v) for v in value.tolist()]
    if isinstance(value, pd.Timestamp):
        return value.isoformat() if pd.notna(value) else None
    if hasattr(value, "to_dict"):
        try:
            return _clean_json(value.to_dict())
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def _J(value: Any) -> Json:
    """psycopg JSON wrapper with NaN/Inf stripped recursively."""
    return Json(_clean_json(value) if value is not None else {})


# ---------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------
def _to_date(v: Any) -> date | None:
    if not v or v in ("0000-00-00", "N/A"):
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _to_num(v: Any) -> float | None:
    if v is None or v == "" or v == "N/A":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v: Any) -> int | None:
    n = _to_num(v)
    return int(n) if n is not None else None


# ---------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------
class Ingestor:
    def __init__(self, api: APIClient | None = None) -> None:
        self.api = api or APIClient(settings.eodhd_api_key)

    # ---- ingest_log helpers ----
    def _log_start(self, endpoint: str, ticker: str | None = None,
                   params: dict | None = None) -> int:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ingest_log (endpoint, ticker, params) "
                    "VALUES (%s,%s,%s) RETURNING id",
                    (endpoint, ticker, _J(params or {})),
                )
                row = cur.fetchone()
            conn.commit()
        return row["id"]

    def _log_end(self, log_id: int, rows: int,
                 status: str = "ok", err: str | None = None) -> None:
        execute(
            "UPDATE ingest_log SET finished_at=now(), status=%s, "
            "rows_written=%s, error_message=%s WHERE id=%s",
            (status, rows, err, log_id),
        )

    # =================================================================
    # REFERENCE
    # =================================================================
    def ingest_exchanges(self) -> int:
        log_id = self._log_start("exchanges")
        try:
            rows = _as_list(self.api.get_exchanges())
            params = [
                (
                    r.get("Code"), r.get("Name"), r.get("OperatingMIC"),
                    r.get("Country"), r.get("Currency"), r.get("CountryISO2"),
                    _J(r),
                )
                for r in rows if r.get("Code")
            ]
            execute_many(
                """INSERT INTO exchanges (code,name,operating_mic,country,
                                          currency,country_iso2,raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (code) DO UPDATE SET
                     name=EXCLUDED.name, operating_mic=EXCLUDED.operating_mic,
                     country=EXCLUDED.country, currency=EXCLUDED.currency,
                     country_iso2=EXCLUDED.country_iso2, raw=EXCLUDED.raw,
                     updated_at=now()""",
                params,
            )
            self._log_end(log_id, len(params))
            log.info("exchanges: %d rows", len(params))
            return len(params)
        except Exception as e:  # noqa: BLE001
            self._log_end(log_id, 0, "error", str(e))
            raise

    def ingest_exchange_details(self, exchange: str) -> None:
        d = _as_dict(self.api.get_details_trading_hours_stock_market_holidays(code=exchange))
        if not d:
            return
        execute(
            """INSERT INTO exchange_details (exchange_code,timezone,
                                             trading_hours,holidays,raw)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (exchange_code) DO UPDATE SET
                 timezone=EXCLUDED.timezone, trading_hours=EXCLUDED.trading_hours,
                 holidays=EXCLUDED.holidays, raw=EXCLUDED.raw, updated_at=now()""",
            (
                exchange,
                d.get("Timezone"),
                _J(d.get("TradingHours") or {}),
                _J(d.get("ExchangeHolidays") or {}),
                _J(d),
            ),
        )

    def ingest_symbols(self, exchange: str) -> int:
        log_id = self._log_start("symbols", params={"exchange": exchange})
        try:
            rows = _as_list(self.api.get_exchange_symbols(exchange))
            params = []
            for r in rows:
                code = r.get("Code")
                if not code:
                    continue
                ticker = f"{code}.{exchange}"
                params.append((
                    ticker, code, exchange, r.get("Name"),
                    r.get("Country"), r.get("Currency"), r.get("Type"),
                    r.get("Isin") or r.get("ISIN"), True,
                ))
            execute_many(
                """INSERT INTO symbols (ticker,code,exchange_code,name,country,
                                        currency,type,isin,is_active)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (ticker) DO UPDATE SET
                     name=EXCLUDED.name, country=EXCLUDED.country,
                     currency=EXCLUDED.currency, type=EXCLUDED.type,
                     isin=EXCLUDED.isin, is_active=EXCLUDED.is_active,
                     updated_at=now()""",
                params,
            )
            self._log_end(log_id, len(params))
            log.info("symbols(%s): %d rows", exchange, len(params))
            return len(params)
        except Exception as e:  # noqa: BLE001
            self._log_end(log_id, 0, "error", str(e))
            raise

    def ensure_symbol(self, ticker: str) -> None:
        """Make sure ``ticker`` exists in symbols (for ad-hoc inserts)."""
        if "." not in ticker:
            raise ValueError(f"ticker {ticker!r} must look like CODE.EXCHANGE")
        code, exch = ticker.rsplit(".", 1)
        # Make sure the exchange row exists - best-effort.
        execute(
            "INSERT INTO exchanges (code,name) VALUES (%s,%s) "
            "ON CONFLICT (code) DO NOTHING",
            (exch, exch),
        )
        execute(
            """INSERT INTO symbols (ticker,code,exchange_code,is_active)
               VALUES (%s,%s,%s,TRUE)
               ON CONFLICT (ticker) DO NOTHING""",
            (ticker, code, exch),
        )

    # ---- Search / lookup ------------------------------------------------
    SEARCH_URL = "https://eodhd.com/api/search/{query}"

    def search_symbols(self, query: str, limit: int = 30,
                       bonds_only: bool = False) -> list[dict]:
        """Resolve a free-text query (company name or partial symbol) to a list
        of candidate instruments using EODHD's Search API.

        Returns a list of dicts with normalised keys:
            ticker (CODE.EXCHANGE), code, exchange, name, type, country,
            currency, isin, previous_close
        Ordered by EODHD's own relevance (popularity / market cap / volume).
        Falls back to an empty list on any network/API error so callers can
        degrade gracefully.
        """
        query = (query or "").strip()
        if not query:
            return []
        url = self.SEARCH_URL.format(query=query)
        try:
            resp = requests.get(
                url,
                params={
                    "api_token": settings.eodhd_api_key,
                    "fmt": "json",
                    "limit": limit,
                    "type": "bond" if bonds_only else "all",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001 - network/json/HTTP all degrade the same
            log.warning("search_symbols(%r) failed: %s", query, e)
            return []
        if not isinstance(data, list):
            return []

        out: list[dict] = []
        for r in data:
            if not isinstance(r, dict):
                continue
            code = r.get("Code")
            exch = r.get("Exchange")
            if not code or not exch:
                continue
            out.append({
                "ticker": f"{code}.{exch}",
                "code": code,
                "exchange": exch,
                "name": r.get("Name"),
                "type": r.get("Type"),
                "country": r.get("Country"),
                "currency": r.get("Currency"),
                "isin": r.get("ISIN") or r.get("Isin"),
                "previous_close": _to_num(r.get("previousClose")),
            })
        return out

    def resolve_ticker(self, query: str) -> str | None:
        """Best-effort single-result resolution of a query to CODE.EXCHANGE.

        If the query already looks like a fully-qualified ticker
        (e.g. 'AAPL.US') it's returned as-is (upper-cased). Otherwise the
        top Search API hit is used. Returns None when nothing matches.
        """
        query = (query or "").strip()
        if not query:
            return None
        if "." in query and " " not in query:
            # Looks already-qualified: SYMBOL.EXCHANGE
            return query.upper()
        hits = self.search_symbols(query, limit=1)
        return hits[0]["ticker"] if hits else None

    def ingest_all_for_query(self, query: str) -> str:
        """Resolve a free-text query to a ticker, then ingest everything.

        Returns the resolved ticker. Raises ValueError if nothing matched.
        """
        ticker = self.resolve_ticker(query)
        if not ticker:
            raise ValueError(f"No instrument found for {query!r}")
        self.ingest_all_for_ticker(ticker)
        return ticker

    # =================================================================
    # PRICES
    # =================================================================
    def ingest_eod(self, ticker: str, from_date: str | None = None,
                   to_date: str | None = None) -> int:
        self.ensure_symbol(ticker)
        log_id = self._log_start("eod", ticker, {"from": from_date, "to": to_date})
        try:
            rows = _as_list(self.api.get_eod_historical_stock_market_data(
                symbol=ticker, period="d",
                from_date=from_date or "1900-01-01",
                to_date=to_date,
                order="a",
            ))
            params = [
                (
                    ticker, _to_date(r.get("date")),
                    _to_num(r.get("open")), _to_num(r.get("high")),
                    _to_num(r.get("low")),  _to_num(r.get("close")),
                    _to_num(r.get("adjusted_close")),
                    _to_int(r.get("volume")),
                )
                for r in rows if r.get("date")
            ]
            execute_many(
                """INSERT INTO eod_prices (ticker,date,open,high,low,close,
                                           adjusted_close,volume)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (ticker,date) DO UPDATE SET
                     open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                     close=EXCLUDED.close, adjusted_close=EXCLUDED.adjusted_close,
                     volume=EXCLUDED.volume""",
                params,
            )
            self._log_end(log_id, len(params))
            log.info("eod(%s): %d rows", ticker, len(params))
            return len(params)
        except Exception as e:  # noqa: BLE001
            self._log_end(log_id, 0, "error", str(e))
            raise

    def ingest_intraday(self, ticker: str, interval: str = "5m") -> int:
        self.ensure_symbol(ticker)
        rows = _as_list(self.api.get_intraday_historical_data(ticker, interval))
        params = []
        for r in rows:
            ts_raw = r.get("datetime") or r.get("date")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            params.append((
                ticker, ts, interval,
                _to_num(r.get("open")),  _to_num(r.get("high")),
                _to_num(r.get("low")),   _to_num(r.get("close")),
                _to_int(r.get("volume")),
            ))
        execute_many(
            """INSERT INTO intraday_prices (ticker,ts,interval,open,high,low,close,volume)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,ts,interval) DO UPDATE SET
                 open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                 close=EXCLUDED.close, volume=EXCLUDED.volume""",
            params,
        )
        return len(params)

    def ingest_live(self, ticker: str) -> None:
        self.ensure_symbol(ticker)
        q = _as_dict(self.api.get_live_stock_prices(ticker))
        if not q or not isinstance(q, dict):
            return
        ts_raw = q.get("timestamp")
        try:
            ts = datetime.fromtimestamp(int(ts_raw)) if ts_raw else datetime.utcnow()
        except (TypeError, ValueError):
            ts = dt.datetime.now(dt.UTC)
        execute(
            """INSERT INTO realtime_quotes (ticker,ts,open,high,low,close,
                                            previous_close,change,change_pct,volume)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker) DO UPDATE SET
                 ts=EXCLUDED.ts, open=EXCLUDED.open, high=EXCLUDED.high,
                 low=EXCLUDED.low, close=EXCLUDED.close,
                 previous_close=EXCLUDED.previous_close, change=EXCLUDED.change,
                 change_pct=EXCLUDED.change_pct, volume=EXCLUDED.volume,
                 updated_at=now()""",
            (
                ticker, ts,
                _to_num(q.get("open")),  _to_num(q.get("high")),
                _to_num(q.get("low")),   _to_num(q.get("close")),
                _to_num(q.get("previousClose")), _to_num(q.get("change")),
                _to_num(q.get("change_p")), _to_int(q.get("volume")),
            ),
        )

    # =================================================================
    # CORPORATE ACTIONS
    # =================================================================
    def ingest_dividends(self, ticker: str) -> int:
        self.ensure_symbol(ticker)
        rows = _as_list(self.api.get_historical_dividends_data(ticker))
        params = []
        for r in rows:
            ex = _to_date(r.get("date"))
            if not ex:
                continue
            params.append((
                ticker, ex,
                _to_date(r.get("declarationDate")),
                _to_date(r.get("recordDate")),
                _to_date(r.get("paymentDate")),
                r.get("period"),
                _to_num(r.get("value")),
                _to_num(r.get("unadjustedValue")),
                r.get("currency"),
            ))
        execute_many(
            """INSERT INTO dividends (ticker,ex_date,declaration_date,record_date,
                                      payment_date,period,value,unadjusted_value,currency)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,ex_date) DO UPDATE SET
                 declaration_date=EXCLUDED.declaration_date,
                 record_date=EXCLUDED.record_date,
                 payment_date=EXCLUDED.payment_date,
                 period=EXCLUDED.period,
                 value=EXCLUDED.value,
                 unadjusted_value=EXCLUDED.unadjusted_value,
                 currency=EXCLUDED.currency""",
            params,
        )
        return len(params)

    def ingest_splits(self, ticker: str) -> int:
        self.ensure_symbol(ticker)
        rows = _as_list(self.api.get_historical_splits_data(ticker))
        params = []
        for r in rows:
            d = _to_date(r.get("date"))
            if not d:
                continue
            split = r.get("split", "") or ""
            numer, denom = None, None
            if "/" in split:
                try:
                    a, b = split.split("/", 1)
                    numer, denom = _to_num(a), _to_num(b)
                except Exception:  # noqa: BLE001
                    pass
            params.append((ticker, d, split, numer, denom))
        execute_many(
            """INSERT INTO splits (ticker,date,split_text,ratio_numer,ratio_denom)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,date) DO UPDATE SET
                 split_text=EXCLUDED.split_text,
                 ratio_numer=EXCLUDED.ratio_numer,
                 ratio_denom=EXCLUDED.ratio_denom""",
            params,
        )
        return len(params)

    # =================================================================
    # FUNDAMENTALS
    # =================================================================
    def ingest_fundamentals(self, ticker: str) -> None:
        self.ensure_symbol(ticker)
        f = _as_dict(self.api.get_fundamentals_data(ticker))
        if not f:
            log.warning("fundamentals(%s): empty", ticker)
            return

        gen = f.get("General") or {}
        hi = f.get("Highlights") or {}

        execute(
            """INSERT INTO fundamentals (
                ticker, asset_type, name, description, sector, industry,
                gic_sector, gic_industry, country, country_iso, currency,
                web_url, logo_url, full_time_employees, ipo_date,
                fiscal_year_end, cik, isin, primary_ticker, is_delisted,
                market_cap, ebitda, pe_ratio, peg_ratio, eps, book_value,
                dividend_share, dividend_yield, profit_margin, operating_margin,
                return_on_assets, return_on_equity, revenue_ttm, gross_profit_ttm,
                quarterly_revenue_growth, quarterly_earnings_growth,
                wall_street_target_price,
                general, highlights, valuation, shares_stats, technicals,
                splits_dividends, analyst_ratings, holders, insider_transactions,
                esg_scores, outstanding_shares, earnings, financials,
                etf_data, components)
               VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,
                       %s,%s,%s,%s, %s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s, %s,%s,%s,%s,
                       %s,%s,%s,%s, %s,%s, %s,
                       %s,%s,%s,%s,%s, %s,%s,%s,%s,
                       %s,%s,%s,%s, %s,%s)
               ON CONFLICT (ticker) DO UPDATE SET
                 asset_type=EXCLUDED.asset_type, name=EXCLUDED.name,
                 description=EXCLUDED.description,
                 sector=EXCLUDED.sector, industry=EXCLUDED.industry,
                 gic_sector=EXCLUDED.gic_sector, gic_industry=EXCLUDED.gic_industry,
                 country=EXCLUDED.country, country_iso=EXCLUDED.country_iso,
                 currency=EXCLUDED.currency, web_url=EXCLUDED.web_url,
                 logo_url=EXCLUDED.logo_url,
                 full_time_employees=EXCLUDED.full_time_employees,
                 ipo_date=EXCLUDED.ipo_date, fiscal_year_end=EXCLUDED.fiscal_year_end,
                 cik=EXCLUDED.cik, isin=EXCLUDED.isin,
                 primary_ticker=EXCLUDED.primary_ticker, is_delisted=EXCLUDED.is_delisted,
                 market_cap=EXCLUDED.market_cap, ebitda=EXCLUDED.ebitda,
                 pe_ratio=EXCLUDED.pe_ratio, peg_ratio=EXCLUDED.peg_ratio,
                 eps=EXCLUDED.eps, book_value=EXCLUDED.book_value,
                 dividend_share=EXCLUDED.dividend_share, dividend_yield=EXCLUDED.dividend_yield,
                 profit_margin=EXCLUDED.profit_margin, operating_margin=EXCLUDED.operating_margin,
                 return_on_assets=EXCLUDED.return_on_assets,
                 return_on_equity=EXCLUDED.return_on_equity,
                 revenue_ttm=EXCLUDED.revenue_ttm,
                 gross_profit_ttm=EXCLUDED.gross_profit_ttm,
                 quarterly_revenue_growth=EXCLUDED.quarterly_revenue_growth,
                 quarterly_earnings_growth=EXCLUDED.quarterly_earnings_growth,
                 wall_street_target_price=EXCLUDED.wall_street_target_price,
                 general=EXCLUDED.general, highlights=EXCLUDED.highlights,
                 valuation=EXCLUDED.valuation, shares_stats=EXCLUDED.shares_stats,
                 technicals=EXCLUDED.technicals, splits_dividends=EXCLUDED.splits_dividends,
                 analyst_ratings=EXCLUDED.analyst_ratings, holders=EXCLUDED.holders,
                 insider_transactions=EXCLUDED.insider_transactions,
                 esg_scores=EXCLUDED.esg_scores, outstanding_shares=EXCLUDED.outstanding_shares,
                 earnings=EXCLUDED.earnings, financials=EXCLUDED.financials,
                 etf_data=EXCLUDED.etf_data, components=EXCLUDED.components,
                 updated_at=now()""",
            (
                ticker,
                gen.get("Type"), gen.get("Name"), gen.get("Description"),
                gen.get("Sector"), gen.get("Industry"),
                gen.get("GicSector"), gen.get("GicIndustry"),
                gen.get("CountryName"), gen.get("CountryISO"), gen.get("CurrencyCode"),
                gen.get("WebURL"), gen.get("LogoURL"),
                _to_int(gen.get("FullTimeEmployees")), _to_date(gen.get("IPODate")),
                gen.get("FiscalYearEnd"), gen.get("CIK"), gen.get("ISIN"),
                gen.get("PrimaryTicker"), bool(gen.get("IsDelisted")),
                _to_num(hi.get("MarketCapitalization")), _to_num(hi.get("EBITDA")),
                _to_num(hi.get("PERatio")), _to_num(hi.get("PEGRatio")),
                _to_num(hi.get("EarningsShare")), _to_num(hi.get("BookValue")),
                _to_num(hi.get("DividendShare")), _to_num(hi.get("DividendYield")),
                _to_num(hi.get("ProfitMargin")), _to_num(hi.get("OperatingMarginTTM")),
                _to_num(hi.get("ReturnOnAssetsTTM")), _to_num(hi.get("ReturnOnEquityTTM")),
                _to_num(hi.get("RevenueTTM")), _to_num(hi.get("GrossProfitTTM")),
                _to_num(hi.get("QuarterlyRevenueGrowthYOY")),
                _to_num(hi.get("QuarterlyEarningsGrowthYOY")),
                _to_num(hi.get("WallStreetTargetPrice")),
                _J(gen), _J(hi), _J(f.get("Valuation") or {}),
                _J(f.get("SharesStats") or {}), _J(f.get("Technicals") or {}),
                _J(f.get("SplitsDividends") or {}),
                _J(f.get("AnalystRatings") or {}), _J(f.get("Holders") or {}),
                _J(f.get("InsiderTransactions") or {}),
                _J(f.get("ESGScores") or {}),
                _J(f.get("outstandingShares") or {}),
                _J(f.get("Earnings") or {}),
                _J(f.get("Financials") or {}),
                _J(f.get("ETF_Data") or {}),
                _J(f.get("Components") or {}),
            ),
        )

        fin = f.get("Financials") or {}
        self._ingest_income(ticker, fin.get("Income_Statement") or {})
        self._ingest_balance(ticker, fin.get("Balance_Sheet") or {})
        self._ingest_cashflow(ticker, fin.get("Cash_Flow") or {})
        self._ingest_earnings_history(ticker, f.get("Earnings") or {})
        self._ingest_earnings_trend(ticker, f.get("Earnings") or {})
        self._ingest_outstanding_shares(ticker, f.get("outstandingShares") or {})
        self._ingest_holders(ticker, f.get("Holders") or {})
        # Insider transactions now come from the dedicated endpoint via
        # ingest_insider() -- it carries the SEC filing date, relationship and
        # title that the fundamentals InsiderTransactions block lacks. The lean
        # block is still stored as jsonb above for provenance/backfill.

        log.info("fundamentals(%s): ok", ticker)

    def _ingest_income(self, ticker: str, payload: dict) -> None:
        for period_type in ("yearly", "quarterly"):
            block = payload.get(period_type) or {}
            params = []
            for d_str, row in block.items():
                d = _to_date(d_str)
                if not d:
                    continue
                params.append((
                    ticker, d, period_type,
                    _to_date(row.get("filing_date")), row.get("currency_symbol"),
                    _to_num(row.get("totalRevenue")), _to_num(row.get("costOfRevenue")),
                    _to_num(row.get("grossProfit")), _to_num(row.get("researchDevelopment")),
                    _to_num(row.get("sellingGeneralAdministrative")),
                    _to_num(row.get("totalOperatingExpenses")),
                    _to_num(row.get("operatingIncome")),
                    _to_num(row.get("interestExpense")),
                    _to_num(row.get("incomeBeforeTax")),
                    _to_num(row.get("incomeTaxExpense")),
                    _to_num(row.get("netIncome")),
                    _to_num(row.get("ebit")), _to_num(row.get("ebitda")),
                    _J(row),
                ))
            execute_many(
                """INSERT INTO income_statements (ticker,date,period_type,filing_date,currency,
                                                  total_revenue,cost_of_revenue,gross_profit,
                                                  research_development,selling_general_admin,
                                                  total_operating_expenses,operating_income,
                                                  interest_expense,income_before_tax,
                                                  income_tax_expense,net_income,ebit,ebitda,raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (ticker,date,period_type) DO UPDATE SET raw=EXCLUDED.raw""",
                params,
            )

    def _ingest_balance(self, ticker: str, payload: dict) -> None:
        for period_type in ("yearly", "quarterly"):
            block = payload.get(period_type) or {}
            params = []
            for d_str, row in block.items():
                d = _to_date(d_str)
                if not d:
                    continue
                params.append((
                    ticker, d, period_type,
                    _to_date(row.get("filing_date")), row.get("currency_symbol"),
                    _to_num(row.get("totalAssets")), _to_num(row.get("totalCurrentAssets")),
                    _to_num(row.get("cash")), _to_num(row.get("shortTermInvestments")),
                    _to_num(row.get("netReceivables")), _to_num(row.get("inventory")),
                    _to_num(row.get("totalLiab")), _to_num(row.get("totalCurrentLiabilities")),
                    _to_num(row.get("longTermDebt")), _to_num(row.get("shortTermDebt")),
                    _to_num(row.get("totalStockholderEquity")),
                    _to_num(row.get("retainedEarnings")), _to_num(row.get("commonStock")),
                    _J(row),
                ))
            execute_many(
                """INSERT INTO balance_sheets (ticker,date,period_type,filing_date,currency,
                                               total_assets,total_current_assets,cash,
                                               short_term_investments,net_receivables,inventory,
                                               total_liab,total_current_liabilities,long_term_debt,
                                               short_term_debt,total_stockholder_equity,
                                               retained_earnings,common_stock,raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (ticker,date,period_type) DO UPDATE SET raw=EXCLUDED.raw""",
                params,
            )

    def _ingest_cashflow(self, ticker: str, payload: dict) -> None:
        for period_type in ("yearly", "quarterly"):
            block = payload.get(period_type) or {}
            params = []
            for d_str, row in block.items():
                d = _to_date(d_str)
                if not d:
                    continue
                params.append((
                    ticker, d, period_type,
                    _to_date(row.get("filing_date")), row.get("currency_symbol"),
                    _to_num(row.get("totalCashFromOperatingActivities")),
                    _to_num(row.get("totalCashflowsFromInvestingActivities")),
                    _to_num(row.get("totalCashFromFinancingActivities")),
                    _to_num(row.get("capitalExpenditures")),
                    _to_num(row.get("freeCashFlow")),
                    _to_num(row.get("dividendsPaid")),
                    _to_num(row.get("salePurchaseOfStock")),
                    _to_num(row.get("changeInCash")),
                    _J(row),
                ))
            execute_many(
                """INSERT INTO cash_flow_statements (ticker,date,period_type,filing_date,currency,
                                                     operating_cash_flow,investing_cash_flow,
                                                     financing_cash_flow,capital_expenditures,
                                                     free_cash_flow,dividends_paid,stock_repurchase,
                                                     change_in_cash,raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (ticker,date,period_type) DO UPDATE SET raw=EXCLUDED.raw""",
                params,
            )

    def _ingest_earnings_history(self, ticker: str, earnings: dict) -> None:
        hist = earnings.get("History") or {}
        params = []
        for k, row in hist.items():
            rd = _to_date(row.get("reportDate") or k)
            if not rd:
                continue
            params.append((
                ticker, rd, _to_date(row.get("date")),
                row.get("beforeAfterMarket"), row.get("currency"),
                _to_num(row.get("epsActual")), _to_num(row.get("epsEstimate")),
                _to_num(row.get("epsDifference")), _to_num(row.get("surprisePercent")),
            ))
        execute_many(
            """INSERT INTO earnings_history (ticker,report_date,date,before_after_market,
                                             currency,eps_actual,eps_estimate,
                                             eps_difference,surprise_pct)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,report_date) DO UPDATE SET
                 date=EXCLUDED.date,
                 eps_actual=EXCLUDED.eps_actual, eps_estimate=EXCLUDED.eps_estimate,
                 eps_difference=EXCLUDED.eps_difference,
                 surprise_pct=EXCLUDED.surprise_pct""",
            params,
        )

    def _ingest_earnings_trend(self, ticker: str, earnings: dict) -> None:
        # v1.1 separates Quarterly/Annual; pre-v1.1 flat list.
        trend = earnings.get("Trend") or {}
        rows: list[dict] = []
        if isinstance(trend, dict):
            for k in ("Quarterly", "Annual"):
                sub = trend.get(k)
                if isinstance(sub, dict):
                    rows.extend(sub.values())
                elif isinstance(sub, list):
                    rows.extend(sub)
            if not rows and all(isinstance(v, dict) for v in trend.values()):
                rows = list(trend.values())
        elif isinstance(trend, list):
            rows = trend

        params = []
        for r in rows:
            d = _to_date(r.get("date"))
            period = r.get("period")
            if not d or not period:
                continue
            params.append((
                ticker, d, period,
                _to_num(r.get("growth")),
                _to_num(r.get("earningsEstimateAvg")),
                _to_num(r.get("earningsEstimateLow")),
                _to_num(r.get("earningsEstimateHigh")),
                _to_num(r.get("revenueEstimateAvg")),
                _to_num(r.get("revenueEstimateLow")),
                _to_num(r.get("revenueEstimateHigh")),
            ))
        execute_many(
            """INSERT INTO earnings_trend (ticker,date,period,growth,
                                           earnings_estimate_avg,earnings_estimate_low,
                                           earnings_estimate_high,revenue_estimate_avg,
                                           revenue_estimate_low,revenue_estimate_high)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,date,period) DO UPDATE SET
                 growth=EXCLUDED.growth,
                 earnings_estimate_avg=EXCLUDED.earnings_estimate_avg,
                 earnings_estimate_low=EXCLUDED.earnings_estimate_low,
                 earnings_estimate_high=EXCLUDED.earnings_estimate_high,
                 revenue_estimate_avg=EXCLUDED.revenue_estimate_avg,
                 revenue_estimate_low=EXCLUDED.revenue_estimate_low,
                 revenue_estimate_high=EXCLUDED.revenue_estimate_high""",
            params,
        )

    def _ingest_outstanding_shares(self, ticker: str, payload: dict) -> None:
        params = []
        for freq in ("annual", "quarterly"):
            block = payload.get(freq) or {}
            for _, row in block.items():
                d = _to_date(row.get("dateFormatted"))
                if not d:
                    continue
                params.append((ticker, d, freq, _to_num(row.get("shares"))))
        execute_many(
            """INSERT INTO shares_outstanding (ticker,date,frequency,shares)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (ticker,date,frequency) DO UPDATE SET shares=EXCLUDED.shares""",
            params,
        )

    def _ingest_holders(self, ticker: str, holders: dict) -> None:
        # EODHD Holders fields (per their fundamentals glossary):
        #   totalShares   -> percentage of the company's shares held  (pct_shares)
        #   totalAssets   -> percentage of the holder's assets here    (pct_assets)
        #   currentShares -> raw number of shares held                 (shares_held)
        inst = holders.get("Institutions") or {}
        iparams = []
        for row in inst.values():
            if not isinstance(row, dict) or not row.get("name"):
                continue
            iparams.append((
                ticker, row.get("name"), _to_date(row.get("date")),
                _to_num(row.get("currentShares")),   # shares_held (count)
                _to_num(row.get("totalShares")),      # pct_shares  (% of company)
                _to_num(row.get("totalAssets")),      # pct_assets  (% of holder assets)
                _to_num(row.get("change")),           # change_shares (EODHD per-holder delta)
                _to_num(row.get("change_p")),         # change_pct
            ))
        execute_many(
            """INSERT INTO institutional_holders (ticker,holder_name,report_date,
                                                  shares_held,pct_shares,pct_assets,
                                                  change_shares,change_pct)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,holder_name,report_date) DO UPDATE SET
                 shares_held=EXCLUDED.shares_held,
                 pct_shares=EXCLUDED.pct_shares,
                 pct_assets=EXCLUDED.pct_assets,
                 change_shares=EXCLUDED.change_shares,
                 change_pct=EXCLUDED.change_pct""",
            iparams,
        )

        funds = holders.get("Funds") or {}
        fparams = []
        for row in funds.values():
            if not isinstance(row, dict) or not row.get("name"):
                continue
            fparams.append((
                ticker, row.get("name"), _to_date(row.get("date")),
                _to_num(row.get("currentShares")),   # shares_held (count)
                _to_num(row.get("totalShares")),      # pct_shares  (% of company)
                _to_num(row.get("change")),           # change_shares (EODHD per-holder delta)
                _to_num(row.get("change_p")),         # change_pct
            ))
        execute_many(
            """INSERT INTO fund_holders (ticker,holder_name,report_date,shares_held,pct_shares,
                                         change_shares,change_pct)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,holder_name,report_date) DO UPDATE SET
                 shares_held=EXCLUDED.shares_held,
                 pct_shares=EXCLUDED.pct_shares,
                 change_shares=EXCLUDED.change_shares,
                 change_pct=EXCLUDED.change_pct""",
            fparams,
        )

    def ingest_insider(self, ticker: str, from_date: str | None = None,
                       to_date: str | None = None, limit: int = 1000) -> int:
        """Pull SEC Form 4 insider transactions from the dedicated endpoint.

        Replaces the old fundamentals-block path, which read three keys that do
        not exist in that block (``transactionShares`` -> shares landed 0.00;
        ``transactionAcquiredDisposedCode`` -> null; and stored
        ``postTransactionAmount`` in the value column). The dedicated endpoint's
        real keys are ``transactionAmount`` / ``transactionPrice`` /
        ``transactionAcquiredDisposed``, and it additionally carries the SEC
        filing date (``reportDate``), ``ownerRelationship`` and ``ownerTitle``.

        There is no dollar-value field in either source, so ``value`` is derived
        as transactionAmount * transactionPrice.

        Costs 10 API calls per request. New rows are inserted complete; rows that
        already exist (e.g. from the old block path) are enriched in place with
        report_date / owner_title / relationship and any repaired shares/value,
        so a re-pull upgrades history without duplicating it.
        """
        self.ensure_symbol(ticker)
        rows = _as_list(self.api.get_insider_transactions_data(
            code=ticker, date_from=from_date, date_to=to_date, limit=limit))

        ins_params, upd_params = [], []
        for row in rows:
            td = _to_date(row.get("transactionDate") or row.get("date"))
            if not td:
                continue
            shares = _to_num(row.get("transactionAmount"))
            price = _to_num(row.get("transactionPrice"))
            value = shares * price if (shares is not None and price is not None) else None
            report_date = _to_date(row.get("reportDate"))
            owner_name = row.get("ownerName")
            code = row.get("transactionCode")
            acq = row.get("transactionAcquiredDisposed")
            title = row.get("ownerTitle")
            relationship = row.get("ownerRelationship")

            ins_params.append((
                ticker, td,
                row.get("ownerCik"), owner_name,
                relationship, code, acq,
                shares, price, value,
                report_date, title,
            ))
            # Enrichment key mirrors backfill_insider.sql: match pre-existing
            # rows on the fields the old bug left intact (date/owner/code/price).
            upd_params.append((
                report_date, title, relationship, acq, shares, price, value,
                ticker, td, owner_name, code, price,
            ))

        if not ins_params:
            return 0

        execute_many(
            """INSERT INTO insider_transactions
                   (ticker,transaction_date,owner_cik,owner_name,
                    relationship,transaction_code,acquisition_or_disposition,
                    shares,price,value,report_date,owner_title)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            ins_params,
        )
        # INSERT skipped rows that already existed; enrich those in place.
        execute_many(
            """UPDATE insider_transactions t SET
                   report_date = COALESCE(t.report_date, %s),
                   owner_title = COALESCE(t.owner_title, %s),
                   relationship = COALESCE(t.relationship, %s),
                   acquisition_or_disposition =
                       COALESCE(t.acquisition_or_disposition, %s),
                   shares = COALESCE(NULLIF(t.shares, 0), %s),
                   price  = COALESCE(t.price, %s),
                   value  = COALESCE(t.value, %s)
               WHERE t.ticker = %s
                 AND t.transaction_date = %s
                 AND t.owner_name IS NOT DISTINCT FROM %s
                 AND t.transaction_code = %s
                 AND round(t.price::numeric, 4) IS NOT DISTINCT FROM round((%s)::numeric, 4)""",
            upd_params,
        )
        return len(ins_params)

    # =================================================================
    # NEWS & SENTIMENT
    # =================================================================
    def ingest_news(self, ticker: str, limit: int = 100) -> int:
        self.ensure_symbol(ticker)
        rows = _as_list(self.api.financial_news(s=ticker, limit=limit))
        params = []
        for r in rows:
            ts_raw = r.get("date")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            sent = r.get("sentiment") or {}
            params.append((
                r.get("uuid"), ticker, ts,
                r.get("title"), r.get("content"), r.get("link"),
                r.get("symbols") or [], r.get("tags") or [],
                _to_num(sent.get("polarity")),
                _to_num(sent.get("neg")), _to_num(sent.get("neu")), _to_num(sent.get("pos")),
            ))
        execute_many(
            """INSERT INTO news (eodhd_uuid,ticker,published_at,title,content,link,
                                 symbols,tags,sentiment_polarity,sentiment_neg,
                                 sentiment_neu,sentiment_pos)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (eodhd_uuid) DO NOTHING""",
            params,
        )
        return len(params)

    def ingest_sentiment(self, ticker: str, from_date: str | None = None,
                         to_date: str | None = None) -> int:
        self.ensure_symbol(ticker)
        data = _as_dict(self.api.get_sentiment(s=ticker, from_date=from_date, to_date=to_date))
        # response shape: {"AAPL.US": [{"date":..., "count":..., "normalized":...}, ...]}
        rows = data.get(ticker) or data.get(ticker.lower()) or []
        params = [
            (
                ticker, _to_date(r.get("date")),
                _to_int(r.get("count")), _to_num(r.get("normalized")),
            )
            for r in rows if r.get("date")
        ]
        execute_many(
            """INSERT INTO sentiment_daily (ticker,date,count,normalized)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (ticker,date) DO UPDATE SET
                 count=EXCLUDED.count, normalized=EXCLUDED.normalized""",
            params,
        )
        return len(params)

    # =================================================================
    # CALENDARS
    # =================================================================
    def ingest_earnings_calendar(self, from_date: str | None = None,
                                 to_date: str | None = None,
                                 symbols: list[str] | None = None) -> int:
        kwargs = {}
        if from_date:
            kwargs["from_date"] = from_date
        if to_date:
            kwargs["to_date"] = to_date
        if symbols:
            kwargs["symbols"] = ",".join(symbols)
        data = _as_dict(self.api.get_upcoming_earnings_data(**kwargs))
        rows = data.get("earnings") or []
        params = []
        for r in rows:
            t = r.get("code")
            rd = _to_date(r.get("report_date"))
            if not t or not rd:
                continue
            params.append((
                t, rd, _to_date(r.get("date")),
                r.get("before_after_market"), r.get("currency"),
                _to_num(r.get("actual")), _to_num(r.get("estimate")),
                _to_num(r.get("difference")), _to_num(r.get("percent")),
            ))
        execute_many(
            """INSERT INTO earnings_calendar (ticker,report_date,date,before_after_market,
                                              currency,eps_actual,eps_estimate,
                                              eps_difference,surprise_pct)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,report_date) DO UPDATE SET
                 date=EXCLUDED.date,
                 eps_actual=EXCLUDED.eps_actual,
                 eps_estimate=EXCLUDED.eps_estimate,
                 eps_difference=EXCLUDED.eps_difference,
                 surprise_pct=EXCLUDED.surprise_pct""",
            params,
        )
        return len(params)

    def ingest_ipo_calendar(self, from_date: str | None = None,
                            to_date: str | None = None) -> int:
        kwargs = {}
        if from_date:
            kwargs["from_date"] = from_date
        if to_date:
            kwargs["to_date"] = to_date
        data = _as_dict(self.api.get_upcoming_IPOs_data(**kwargs))
        rows = data.get("ipos") or []
        params = []
        for r in rows:
            code = r.get("code")
            exch = r.get("exchange")
            sd = _to_date(r.get("start_date"))
            if not code or not exch or not sd:
                continue
            params.append((
                code, exch, r.get("name"), r.get("currency"), sd,
                _to_date(r.get("filing_date")), _to_date(r.get("amended_date")),
                _to_num(r.get("price_from")), _to_num(r.get("price_to")),
                _to_num(r.get("offer_price")), _to_num(r.get("shares")),
                r.get("deal_type"),
            ))
        execute_many(
            """INSERT INTO ipo_calendar (code,exchange,name,currency,start_date,
                                         filing_date,amended_date,price_from,price_to,
                                         offer_price,shares,deal_type)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (code,exchange,start_date) DO UPDATE SET
                 name=EXCLUDED.name, currency=EXCLUDED.currency,
                 filing_date=EXCLUDED.filing_date, amended_date=EXCLUDED.amended_date,
                 price_from=EXCLUDED.price_from, price_to=EXCLUDED.price_to,
                 offer_price=EXCLUDED.offer_price, shares=EXCLUDED.shares,
                 deal_type=EXCLUDED.deal_type""",
            params,
        )
        return len(params)

    def ingest_splits_calendar(self, from_date: str | None = None,
                               to_date: str | None = None) -> int:
        kwargs = {}
        if from_date:
            kwargs["from_date"] = from_date
        if to_date:
            kwargs["to_date"] = to_date
        data = _as_dict(self.api.get_upcoming_splits_data(**kwargs))
        rows = data.get("splits") or []
        params = []
        for r in rows:
            code = r.get("code")
            exch = r.get("exchange")
            sd = _to_date(r.get("split_date"))
            if not code or not exch or not sd:
                continue
            split = r.get("split", "") or ""
            sf, st = None, None
            if "/" in split:
                try:
                    a, b = split.split("/", 1)
                    sf, st = _to_num(a), _to_num(b)
                except Exception:  # noqa: BLE001
                    pass
            params.append((
                code, exch, r.get("name"), sd, bool(r.get("optionable")),
                _to_num(r.get("old_shares")), _to_num(r.get("new_shares")), sf, st,
            ))
        execute_many(
            """INSERT INTO splits_calendar (code,exchange,name,split_date,optionable,
                                            old_shares,new_shares,split_from,split_to)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (code,exchange,split_date) DO UPDATE SET
                 name=EXCLUDED.name, optionable=EXCLUDED.optionable,
                 old_shares=EXCLUDED.old_shares, new_shares=EXCLUDED.new_shares,
                 split_from=EXCLUDED.split_from, split_to=EXCLUDED.split_to""",
            params,
        )
        return len(params)

    # =================================================================
    # MACRO
    # =================================================================
    def ingest_economic_events(self, country: str | None = None,
                               from_date: str | None = None,
                               to_date: str | None = None,
                               limit: int = 1000) -> int:
        kwargs: dict[str, Any] = {"limit": limit}
        if country:
            kwargs["country"] = country
        if from_date:
            kwargs["date_from"] = from_date
        if to_date:
            kwargs["date_to"] = to_date
        rows = _as_list(self.api.get_economic_events_data(**kwargs))
        params = []
        for r in rows:
            try:
                dt = datetime.fromisoformat(str(r.get("date")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            params.append((
                dt, r.get("country"), r.get("type"), r.get("comparison"),
                r.get("period"),
                _to_num(r.get("actual")), _to_num(r.get("previous")),
                _to_num(r.get("estimate")), _to_num(r.get("change")),
                _to_num(r.get("change_percentage")),
            ))
        execute_many(
            """INSERT INTO economic_events (event_date,country,type,comparison,period,
                                            actual,previous,estimate,change,change_pct)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (event_date,country,type,period) DO UPDATE SET
                 comparison=EXCLUDED.comparison,
                 actual=EXCLUDED.actual, previous=EXCLUDED.previous,
                 estimate=EXCLUDED.estimate, change=EXCLUDED.change,
                 change_pct=EXCLUDED.change_pct""",
            params,
        )
        return len(params)

    def ingest_macro_indicator(self, country: str, indicator: str) -> int:
        rows = _as_list(self.api.get_macro_indicators_data(country=country, indicator=indicator))
        params = []
        for r in rows:
            d = _to_date(r.get("Date"))
            if not d:
                continue
            params.append((country, indicator, d, _to_num(r.get("Value"))))
        execute_many(
            """INSERT INTO macro_indicators (country,indicator,date,value)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (country,indicator,date) DO UPDATE SET value=EXCLUDED.value""",
            params,
        )
        return len(params)

    # =================================================================
    # OPTIONS
    # =================================================================
    def ingest_options(self, ticker: str, from_date: str | None = None,
                       to_date: str | None = None) -> int:
        self.ensure_symbol(ticker)
        kwargs = {}
        if from_date:
            kwargs["date_from"] = from_date
        if to_date:
            kwargs["date_to"] = to_date
        data = _as_dict(self.api.get_options_data(ticker, **kwargs))
        snapshot = date.today()
        params = []
        for entry in (data.get("data") or []):
            exp = _to_date(entry.get("expirationDate"))
            if not exp:
                continue
            for side in ("CALL", "PUT"):
                for opt in (entry.get("options", {}).get(side) or []):
                    strike = _to_num(opt.get("strike"))
                    if strike is None:
                        continue
                    try:
                        ltd = datetime.fromisoformat(
                            str(opt.get("lastTradeDateTime")).replace("Z", "+00:00")
                        ) if opt.get("lastTradeDateTime") else None
                    except ValueError:
                        ltd = None
                    params.append((
                        ticker, exp, side, strike, ltd,
                        _to_num(opt.get("lastPrice")), _to_num(opt.get("change")),
                        _to_num(opt.get("changePercent")),
                        _to_num(opt.get("bid")), _to_num(opt.get("ask")),
                        _to_int(opt.get("volume")), _to_int(opt.get("openInterest")),
                        _to_num(opt.get("impliedVolatility")),
                        _to_num(opt.get("delta")), _to_num(opt.get("gamma")),
                        _to_num(opt.get("theta")), _to_num(opt.get("vega")),
                        _to_num(opt.get("rho")),
                        _to_num(opt.get("theoretical")),
                        _to_num(opt.get("intrinsicValue")),
                        _to_num(opt.get("timeValue")),
                        bool(opt.get("inTheMoney")),
                        snapshot,
                    ))
        execute_many(
            """INSERT INTO options_chains (ticker,expiration_date,option_type,strike,
                                           last_trade_date,last,change,change_pct,
                                           bid,ask,volume,open_interest,implied_volatility,
                                           delta,gamma,theta,vega,rho,
                                           theoretical,intrinsic_value,time_value,
                                           in_the_money,snapshot_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker,expiration_date,option_type,strike,snapshot_date)
               DO UPDATE SET
                 last=EXCLUDED.last, bid=EXCLUDED.bid, ask=EXCLUDED.ask,
                 volume=EXCLUDED.volume, open_interest=EXCLUDED.open_interest,
                 implied_volatility=EXCLUDED.implied_volatility""",
            params,
        )
        return len(params)

    # =================================================================
    # "all" convenience
    # =================================================================
    def ingest_all_for_ticker(self, ticker: str) -> None:
        log.info("=== Pulling everything for %s ===", ticker)
        self.ensure_symbol(ticker)
        self.ingest_eod(ticker)
        self.ingest_fundamentals(ticker)
        self.ingest_dividends(ticker)
        self.ingest_splits(ticker)
        try:
            self.ingest_live(ticker)
        except (Exception, SystemExit) as e:  # eodhd may sys.exit() on API errors
            log.warning("live(%s) skipped: %s", ticker, e)
        try:
            self.ingest_news(ticker, limit=50)
        except (Exception, SystemExit) as e:  # eodhd may sys.exit() on API errors
            log.warning("news(%s) skipped: %s", ticker, e)
        try:
            self.ingest_sentiment(ticker)
        except (Exception, SystemExit) as e:  # eodhd may sys.exit() on API errors
            log.warning("sentiment(%s) skipped: %s", ticker, e)
        try:
            self.ingest_insider(ticker)  # dedicated endpoint: 10 API calls
        except (Exception, SystemExit) as e:  # eodhd may sys.exit() on API errors
            log.warning("insider(%s) skipped: %s", ticker, e)

    def refresh_eod_all(self, since: str | None = None) -> None:
        rows = fetch_all("SELECT ticker FROM symbols WHERE is_active=TRUE")
        for r in rows:
            try:
                self.ingest_eod(r["ticker"], from_date=since)
            except Exception as e:  # noqa: BLE001
                log.error("eod refresh failed for %s: %s", r["ticker"], e)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def _cli() -> None:
    p = argparse.ArgumentParser(description="EODHD -> Postgres ingestor")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("exchanges")
    s = sub.add_parser("symbols")
    s.add_argument("exchange")

    s = sub.add_parser("eod")
    s.add_argument("ticker")
    s.add_argument("--from", dest="from_date")
    s.add_argument("--to", dest="to_date")

    s = sub.add_parser("eod-refresh")
    s.add_argument("--since", dest="since")

    s = sub.add_parser("intraday")
    s.add_argument("ticker")
    s.add_argument("--interval", default="5m")

    s = sub.add_parser("fundamentals")
    s.add_argument("ticker")

    s = sub.add_parser("insider")
    s.add_argument("ticker")
    s.add_argument("--from", dest="from_date")
    s.add_argument("--to", dest="to_date")

    s = sub.add_parser("dividends")
    s.add_argument("ticker")

    s = sub.add_parser("splits")
    s.add_argument("ticker")

    s = sub.add_parser("news")
    s.add_argument("tickers", nargs="+")

    s = sub.add_parser("calendar-earnings")
    s.add_argument("--from", dest="from_date")
    s.add_argument("--to", dest="to_date")

    s = sub.add_parser("calendar-ipos")
    s = sub.add_parser("calendar-splits")

    s = sub.add_parser("economic-events")
    s.add_argument("--country")
    s.add_argument("--from", dest="from_date")
    s.add_argument("--to", dest="to_date")

    s = sub.add_parser("macro")
    s.add_argument("country")
    s.add_argument("indicator")

    s = sub.add_parser("options")
    s.add_argument("ticker")

    s = sub.add_parser("all")
    s.add_argument("ticker")
    s.add_argument("--resolve", action="store_true",
                   help="treat the argument as a free-text query (company "
                        "name or partial symbol) and resolve it via the "
                        "Search API before ingesting")

    s = sub.add_parser("search")
    s.add_argument("query", nargs="+", help="company name or partial symbol")
    s.add_argument("--limit", type=int, default=15)

    args = p.parse_args()
    ing = Ingestor()

    if args.cmd == "exchanges":
        ing.ingest_exchanges()
    elif args.cmd == "symbols":
        ing.ingest_symbols(args.exchange)
        ing.ingest_exchange_details(args.exchange)
    elif args.cmd == "eod":
        ing.ingest_eod(args.ticker, args.from_date, args.to_date)
    elif args.cmd == "eod-refresh":
        ing.refresh_eod_all(args.since)
    elif args.cmd == "intraday":
        ing.ingest_intraday(args.ticker, args.interval)
    elif args.cmd == "fundamentals":
        ing.ingest_fundamentals(args.ticker)
    elif args.cmd == "insider":
        ing.ingest_insider(args.ticker, args.from_date, args.to_date)
    elif args.cmd == "dividends":
        ing.ingest_dividends(args.ticker)
    elif args.cmd == "splits":
        ing.ingest_splits(args.ticker)
    elif args.cmd == "news":
        for t in args.tickers:
            ing.ingest_news(t)
    elif args.cmd == "calendar-earnings":
        ing.ingest_earnings_calendar(args.from_date, args.to_date)
    elif args.cmd == "calendar-ipos":
        ing.ingest_ipo_calendar()
    elif args.cmd == "calendar-splits":
        ing.ingest_splits_calendar()
    elif args.cmd == "economic-events":
        ing.ingest_economic_events(args.country, args.from_date, args.to_date)
    elif args.cmd == "macro":
        ing.ingest_macro_indicator(args.country, args.indicator)
    elif args.cmd == "options":
        ing.ingest_options(args.ticker)
    elif args.cmd == "all":
        if getattr(args, "resolve", False):
            resolved = ing.ingest_all_for_query(args.ticker)
            log.info("resolved %r -> %s", args.ticker, resolved)
        else:
            ing.ingest_all_for_ticker(args.ticker)
    elif args.cmd == "search":
        q = " ".join(args.query)
        hits = ing.search_symbols(q, limit=args.limit)
        if not hits:
            print(f"No matches for {q!r}")
        else:
            print(f"{'TICKER':<18} {'TYPE':<14} {'COUNTRY':<10} NAME")
            print("-" * 70)
            for h in hits:
                print(f"{h['ticker']:<18} {(h['type'] or ''):<14} "
                      f"{(h['country'] or ''):<10} {h['name'] or ''}")


if __name__ == "__main__":
    _cli()