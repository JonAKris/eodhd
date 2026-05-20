-- =====================================================================
-- EODHD All-in-One -> Postgres schema + Portfolio Tracker
-- Tested on PostgreSQL 14+
-- =====================================================================
-- How to run:
--    psql -U postgres -f schema.sql
--
-- Conventions:
--   * Tickers stored in EODHD form "SYMBOL.EXCHANGE" (e.g. AAPL.US).
--   * Money columns use NUMERIC (never FLOAT - cents matter).
--   * Frequently queried fundamentals fields are extracted into proper
--     columns; the deeply nested / sparse parts stay as JSONB so we
--     don't have to chase every schema change EODHD ships.
-- =====================================================================

CREATE DATABASE eodhd
    WITH ENCODING 'UTF8'
         LC_COLLATE 'en_US.UTF-8'
         LC_CTYPE   'en_US.UTF-8'
         TEMPLATE template0;

\c eodhd

CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS btree_gin;    -- composite GIN indexes

-- =====================================================================
-- 1. REFERENCE DATA
-- =====================================================================

CREATE TABLE exchanges (
    code            TEXT PRIMARY KEY,            -- 'US', 'LSE', 'XETRA'
    name            TEXT NOT NULL,
    operating_mic   TEXT,
    country         TEXT,
    currency        TEXT,
    country_iso2    TEXT,
    raw             JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE exchange_details (
    exchange_code   TEXT PRIMARY KEY REFERENCES exchanges(code) ON DELETE CASCADE,
    timezone        TEXT,
    trading_hours   JSONB,
    holidays        JSONB,
    raw             JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE symbols (
    ticker          TEXT PRIMARY KEY,            -- 'AAPL.US'
    code            TEXT NOT NULL,               -- 'AAPL'
    exchange_code   TEXT NOT NULL REFERENCES exchanges(code) ON DELETE CASCADE,
    name            TEXT,
    country         TEXT,
    currency        TEXT,
    type            TEXT,                        -- 'Common Stock', 'ETF', ...
    isin            TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    delisted_on     DATE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX symbols_exchange_idx ON symbols(exchange_code);
CREATE INDEX symbols_code_idx     ON symbols(code);
CREATE INDEX symbols_isin_idx     ON symbols(isin) WHERE isin IS NOT NULL;
CREATE INDEX symbols_name_trgm    ON symbols USING gin (name gin_trgm_ops);

-- Note: enable pg_trgm for the gin index on name
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE symbol_change_history (
    id              BIGSERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    old_symbol      TEXT NOT NULL,
    new_symbol      TEXT NOT NULL,
    name            TEXT,
    UNIQUE (date, old_symbol, new_symbol)
);

-- =====================================================================
-- 2. PRICES
-- =====================================================================

CREATE TABLE eod_prices (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date            DATE NOT NULL,
    open            NUMERIC(18,6),
    high            NUMERIC(18,6),
    low             NUMERIC(18,6),
    close           NUMERIC(18,6),
    adjusted_close  NUMERIC(18,6),
    volume          BIGINT,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX eod_prices_date_idx ON eod_prices(date);

CREATE TABLE intraday_prices (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL,
    interval        TEXT NOT NULL,               -- '1m','5m','1h'
    open            NUMERIC(18,6),
    high            NUMERIC(18,6),
    low             NUMERIC(18,6),
    close           NUMERIC(18,6),
    volume          BIGINT,
    PRIMARY KEY (ticker, ts, interval)
);
CREATE INDEX intraday_prices_ts_idx ON intraday_prices(ts);

-- One row per ticker; updated each time we poll the live endpoint.
CREATE TABLE realtime_quotes (
    ticker          TEXT PRIMARY KEY REFERENCES symbols(ticker) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL,
    open            NUMERIC(18,6),
    high            NUMERIC(18,6),
    low             NUMERIC(18,6),
    close           NUMERIC(18,6),
    previous_close  NUMERIC(18,6),
    change          NUMERIC(18,6),
    change_pct      NUMERIC(10,4),
    volume          BIGINT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tick_data (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL,
    price           NUMERIC(18,6),
    size            BIGINT,
    side            TEXT
);
CREATE INDEX tick_data_ticker_ts_idx ON tick_data(ticker, ts);

CREATE TABLE technical_indicators (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    function        TEXT NOT NULL,                -- 'sma','rsi','macd', ...
    period          INTEGER,
    date            DATE NOT NULL,
    value           NUMERIC(20,8),
    extra           JSONB,                        -- for multi-value indicators
    PRIMARY KEY (ticker, function, period, date)
);

-- =====================================================================
-- 3. CORPORATE ACTIONS
-- =====================================================================

CREATE TABLE dividends (
    ticker           TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    ex_date          DATE NOT NULL,
    declaration_date DATE,
    record_date      DATE,
    payment_date     DATE,
    period           TEXT,
    value            NUMERIC(18,6),
    unadjusted_value NUMERIC(18,6),
    currency         TEXT,
    PRIMARY KEY (ticker, ex_date)
);

CREATE TABLE splits (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date            DATE NOT NULL,
    split_text      TEXT,                        -- '4/1'
    ratio_numer     NUMERIC(18,6),
    ratio_denom     NUMERIC(18,6),
    PRIMARY KEY (ticker, date)
);

CREATE TABLE shares_outstanding (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date            DATE NOT NULL,
    frequency       TEXT NOT NULL,                -- 'annual'|'quarterly'
    shares          NUMERIC(20,2),
    PRIMARY KEY (ticker, date, frequency)
);

CREATE TABLE historical_market_cap (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date            DATE NOT NULL,
    market_cap      NUMERIC(24,2),
    PRIMARY KEY (ticker, date)
);

-- =====================================================================
-- 4. FUNDAMENTALS
-- =====================================================================
-- The fundamentals payload varies per asset type (Common Stock / ETF /
-- Fund / Index / Crypto / Bond). We extract the always-queried scalars
-- and keep each top-level section as a JSONB column for everything else.

CREATE TABLE fundamentals (
    ticker              TEXT PRIMARY KEY REFERENCES symbols(ticker) ON DELETE CASCADE,
    asset_type          TEXT,
    -- General
    name                TEXT,
    description         TEXT,
    sector              TEXT,
    industry            TEXT,
    gic_sector          TEXT,
    gic_industry        TEXT,
    country             TEXT,
    country_iso         TEXT,
    currency            TEXT,
    web_url             TEXT,
    logo_url            TEXT,
    full_time_employees INTEGER,
    ipo_date            DATE,
    fiscal_year_end     TEXT,
    cik                 TEXT,
    isin                TEXT,
    primary_ticker      TEXT,
    is_delisted         BOOLEAN,
    -- Highlights (flattened scalars)
    market_cap                  NUMERIC(24,2),
    ebitda                      NUMERIC(24,2),
    pe_ratio                    NUMERIC(18,4),
    peg_ratio                   NUMERIC(18,4),
    eps                         NUMERIC(18,4),
    book_value                  NUMERIC(18,4),
    dividend_share              NUMERIC(18,6),
    dividend_yield              NUMERIC(18,6),
    profit_margin               NUMERIC(18,6),
    operating_margin            NUMERIC(18,6),
    return_on_assets            NUMERIC(18,6),
    return_on_equity            NUMERIC(18,6),
    revenue_ttm                 NUMERIC(24,2),
    gross_profit_ttm            NUMERIC(24,2),
    quarterly_revenue_growth    NUMERIC(18,6),
    quarterly_earnings_growth   NUMERIC(18,6),
    wall_street_target_price    NUMERIC(18,4),
    -- Raw sections (point-in-time snapshot)
    general              JSONB,
    highlights           JSONB,
    valuation            JSONB,
    shares_stats         JSONB,
    technicals           JSONB,
    splits_dividends     JSONB,
    analyst_ratings      JSONB,
    holders              JSONB,
    insider_transactions JSONB,
    esg_scores           JSONB,
    outstanding_shares   JSONB,
    earnings             JSONB,
    financials           JSONB,
    etf_data             JSONB,                   -- ETF/Fund specific
    components           JSONB,                   -- index components
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX fundamentals_sector_idx     ON fundamentals(sector);
CREATE INDEX fundamentals_industry_idx   ON fundamentals(industry);
CREATE INDEX fundamentals_country_idx    ON fundamentals(country);
CREATE INDEX fundamentals_general_gin    ON fundamentals USING GIN (general);
CREATE INDEX fundamentals_financials_gin ON fundamentals USING GIN (financials);

-- Normalised financial statements for easy time-series queries.
CREATE TABLE income_statements (
    ticker              TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date                DATE NOT NULL,
    period_type         TEXT NOT NULL,            -- 'yearly'|'quarterly'
    filing_date         DATE,
    currency            TEXT,
    total_revenue              NUMERIC(24,2),
    cost_of_revenue            NUMERIC(24,2),
    gross_profit               NUMERIC(24,2),
    research_development       NUMERIC(24,2),
    selling_general_admin      NUMERIC(24,2),
    total_operating_expenses   NUMERIC(24,2),
    operating_income           NUMERIC(24,2),
    interest_expense           NUMERIC(24,2),
    income_before_tax          NUMERIC(24,2),
    income_tax_expense         NUMERIC(24,2),
    net_income                 NUMERIC(24,2),
    ebit                       NUMERIC(24,2),
    ebitda                     NUMERIC(24,2),
    raw                 JSONB,
    PRIMARY KEY (ticker, date, period_type)
);

CREATE TABLE balance_sheets (
    ticker              TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date                DATE NOT NULL,
    period_type         TEXT NOT NULL,
    filing_date         DATE,
    currency            TEXT,
    total_assets               NUMERIC(24,2),
    total_current_assets       NUMERIC(24,2),
    cash                       NUMERIC(24,2),
    short_term_investments     NUMERIC(24,2),
    net_receivables            NUMERIC(24,2),
    inventory                  NUMERIC(24,2),
    total_liab                 NUMERIC(24,2),
    total_current_liabilities  NUMERIC(24,2),
    long_term_debt             NUMERIC(24,2),
    short_term_debt            NUMERIC(24,2),
    total_stockholder_equity   NUMERIC(24,2),
    retained_earnings          NUMERIC(24,2),
    common_stock               NUMERIC(24,2),
    raw                 JSONB,
    PRIMARY KEY (ticker, date, period_type)
);

CREATE TABLE cash_flow_statements (
    ticker              TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date                DATE NOT NULL,
    period_type         TEXT NOT NULL,
    filing_date         DATE,
    currency            TEXT,
    operating_cash_flow     NUMERIC(24,2),
    investing_cash_flow     NUMERIC(24,2),
    financing_cash_flow     NUMERIC(24,2),
    capital_expenditures    NUMERIC(24,2),
    free_cash_flow          NUMERIC(24,2),
    dividends_paid          NUMERIC(24,2),
    stock_repurchase        NUMERIC(24,2),
    change_in_cash          NUMERIC(24,2),
    raw                 JSONB,
    PRIMARY KEY (ticker, date, period_type)
);

CREATE TABLE earnings_history (
    ticker              TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    report_date         DATE NOT NULL,
    date                DATE,
    before_after_market TEXT,
    currency            TEXT,
    eps_actual          NUMERIC(18,6),
    eps_estimate        NUMERIC(18,6),
    eps_difference      NUMERIC(18,6),
    surprise_pct        NUMERIC(18,6),
    PRIMARY KEY (ticker, report_date)
);

CREATE TABLE earnings_trend (
    ticker              TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date                DATE NOT NULL,
    period              TEXT NOT NULL,            -- '0q','+1q','0y','+1y'
    growth              NUMERIC(18,6),
    earnings_estimate_avg  NUMERIC(18,6),
    earnings_estimate_low  NUMERIC(18,6),
    earnings_estimate_high NUMERIC(18,6),
    revenue_estimate_avg   NUMERIC(24,2),
    revenue_estimate_low   NUMERIC(24,2),
    revenue_estimate_high  NUMERIC(24,2),
    PRIMARY KEY (ticker, date, period)
);

CREATE TABLE analyst_ratings_history (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date            DATE NOT NULL,
    rating          NUMERIC(6,3),
    target_price    NUMERIC(18,4),
    strong_buy      INTEGER,
    buy             INTEGER,
    hold            INTEGER,
    sell            INTEGER,
    strong_sell     INTEGER,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE institutional_holders (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    holder_name     TEXT NOT NULL,
    report_date     DATE,
    total_shares    NUMERIC(20,2),
    total_assets    NUMERIC(24,2),
    pct_held        NUMERIC(10,6),
    PRIMARY KEY (ticker, holder_name, report_date)
);

CREATE TABLE fund_holders (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    holder_name     TEXT NOT NULL,
    report_date     DATE,
    total_shares    NUMERIC(20,2),
    pct_held        NUMERIC(10,6),
    PRIMARY KEY (ticker, holder_name, report_date)
);

CREATE TABLE insider_transactions (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    transaction_date DATE NOT NULL,
    owner_cik       TEXT,
    owner_name      TEXT,
    relationship    TEXT,
    transaction_code TEXT,
    acquisition_or_disposition TEXT,
    shares          NUMERIC(20,2),
    price           NUMERIC(18,4),
    value           NUMERIC(24,2),
    PRIMARY KEY (ticker, transaction_date, owner_name, transaction_code, shares)
);

-- =====================================================================
-- 5. NEWS & SENTIMENT
-- =====================================================================

CREATE TABLE news (
    id              BIGSERIAL PRIMARY KEY,
    eodhd_uuid      TEXT UNIQUE,
    ticker          TEXT REFERENCES symbols(ticker) ON DELETE SET NULL,
    published_at    TIMESTAMPTZ NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT,
    link            TEXT,
    symbols         TEXT[],
    tags            TEXT[],
    sentiment_polarity NUMERIC(10,6),
    sentiment_neg   NUMERIC(10,6),
    sentiment_neu   NUMERIC(10,6),
    sentiment_pos   NUMERIC(10,6)
);
CREATE INDEX news_ticker_ts_idx ON news(ticker, published_at DESC);
CREATE INDEX news_symbols_gin   ON news USING GIN (symbols);
CREATE INDEX news_tags_gin      ON news USING GIN (tags);

CREATE TABLE sentiment_daily (
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    date            DATE NOT NULL,
    count           INTEGER,
    normalized      NUMERIC(10,6),
    PRIMARY KEY (ticker, date)
);

-- =====================================================================
-- 6. CALENDARS
-- =====================================================================

CREATE TABLE earnings_calendar (
    ticker              TEXT NOT NULL,
    report_date         DATE NOT NULL,
    date                DATE,
    before_after_market TEXT,
    currency            TEXT,
    eps_actual          NUMERIC(18,6),
    eps_estimate        NUMERIC(18,6),
    eps_difference      NUMERIC(18,6),
    surprise_pct        NUMERIC(18,6),
    PRIMARY KEY (ticker, report_date)
);

CREATE TABLE ipo_calendar (
    code            TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    name            TEXT,
    currency        TEXT,
    start_date      DATE,
    filing_date     DATE,
    amended_date    DATE,
    price_from      NUMERIC(18,4),
    price_to        NUMERIC(18,4),
    offer_price     NUMERIC(18,4),
    shares          NUMERIC(20,2),
    deal_type       TEXT,
    PRIMARY KEY (code, exchange, start_date)
);

CREATE TABLE splits_calendar (
    code            TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    name            TEXT,
    split_date      DATE NOT NULL,
    optionable      BOOLEAN,
    old_shares      NUMERIC(20,2),
    new_shares      NUMERIC(20,2),
    split_from      NUMERIC(18,6),
    split_to        NUMERIC(18,6),
    PRIMARY KEY (code, exchange, split_date)
);

-- =====================================================================
-- 7. INDICES & CONSTITUENTS
-- =====================================================================

CREATE TABLE index_constituents (
    index_code      TEXT NOT NULL,            -- 'GSPC.INDX'
    member_code     TEXT NOT NULL,
    member_exchange TEXT,
    member_name     TEXT,
    sector          TEXT,
    industry        TEXT,
    weight          NUMERIC(10,6),
    as_of_date      DATE NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (index_code, member_code, as_of_date)
);

-- =====================================================================
-- 8. MACRO / ECONOMIC
-- =====================================================================

CREATE TABLE economic_events (
    id              BIGSERIAL PRIMARY KEY,
    event_date      TIMESTAMPTZ NOT NULL,
    country         TEXT,
    type            TEXT,
    comparison      TEXT,
    period          TEXT,
    actual          NUMERIC(24,6),
    previous        NUMERIC(24,6),
    estimate        NUMERIC(24,6),
    change          NUMERIC(24,6),
    change_pct      NUMERIC(10,6),
    UNIQUE (event_date, country, type, period)
);
CREATE INDEX economic_events_country_date_idx ON economic_events(country, event_date DESC);

CREATE TABLE macro_indicators (
    country         TEXT NOT NULL,
    indicator       TEXT NOT NULL,
    date            DATE NOT NULL,
    value           NUMERIC(24,6),
    PRIMARY KEY (country, indicator, date)
);

-- =====================================================================
-- 9. OPTIONS
-- =====================================================================

CREATE TABLE options_chains (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    expiration_date DATE NOT NULL,
    option_type     TEXT NOT NULL,                -- 'CALL'|'PUT'
    strike          NUMERIC(18,4) NOT NULL,
    last_trade_date TIMESTAMPTZ,
    last            NUMERIC(18,4),
    change          NUMERIC(18,4),
    change_pct      NUMERIC(10,4),
    bid             NUMERIC(18,4),
    ask             NUMERIC(18,4),
    volume          BIGINT,
    open_interest   BIGINT,
    implied_volatility NUMERIC(18,6),
    delta           NUMERIC(10,6),
    gamma           NUMERIC(10,6),
    theta           NUMERIC(10,6),
    vega            NUMERIC(10,6),
    rho             NUMERIC(10,6),
    theoretical     NUMERIC(18,4),
    intrinsic_value NUMERIC(18,4),
    time_value      NUMERIC(18,4),
    in_the_money    BOOLEAN,
    snapshot_date   DATE NOT NULL,
    UNIQUE (ticker, expiration_date, option_type, strike, snapshot_date)
);
CREATE INDEX options_chains_ticker_exp_idx ON options_chains(ticker, expiration_date);

-- =====================================================================
-- 10. BONDS
-- =====================================================================

CREATE TABLE bond_fundamentals (
    isin            TEXT PRIMARY KEY,
    issuer          TEXT,
    currency        TEXT,
    coupon          NUMERIC(10,6),
    issue_date      DATE,
    maturity_date   DATE,
    face_value      NUMERIC(24,2),
    raw             JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =====================================================================
-- 11. INGEST BOOKKEEPING
-- =====================================================================

CREATE TABLE ingest_log (
    id              BIGSERIAL PRIMARY KEY,
    endpoint        TEXT NOT NULL,
    ticker          TEXT,
    params          JSONB,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'started',  -- started|ok|error
    rows_written    INTEGER,
    error_message   TEXT
);
CREATE INDEX ingest_log_endpoint_idx ON ingest_log(endpoint, started_at DESC);

-- =====================================================================
-- 12. PORTFOLIO TRACKING (user-facing CRUD)
-- =====================================================================

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    display_name    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A default single-user row so the Dash app works without an auth setup.
INSERT INTO users (id, email, display_name)
VALUES ('00000000-0000-0000-0000-000000000001', 'demo@local', 'Demo User');

CREATE TABLE portfolios (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    base_currency   TEXT NOT NULL DEFAULT 'USD',
    initial_cash    NUMERIC(20,2) NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE trades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id    UUID NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    ticker          TEXT NOT NULL REFERENCES symbols(ticker),
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    trade_date      DATE NOT NULL,
    quantity        NUMERIC(20,8) NOT NULL CHECK (quantity > 0),
    price           NUMERIC(20,8) NOT NULL CHECK (price >= 0),
    fees            NUMERIC(20,4) NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'USD',
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX trades_portfolio_idx ON trades(portfolio_id);
CREATE INDEX trades_ticker_idx    ON trades(ticker);
CREATE INDEX trades_date_idx      ON trades(trade_date);

-- View: open positions per (portfolio, ticker), average cost basis on
-- accumulated buys.
CREATE OR REPLACE VIEW portfolio_positions AS
SELECT
    t.portfolio_id,
    t.ticker,
    SUM(CASE WHEN t.side='BUY' THEN t.quantity ELSE -t.quantity END) AS quantity,
    CASE
        WHEN SUM(CASE WHEN t.side='BUY' THEN t.quantity ELSE 0 END) = 0 THEN 0
        ELSE SUM(CASE WHEN t.side='BUY' THEN t.quantity*t.price ELSE 0 END)
             / NULLIF(SUM(CASE WHEN t.side='BUY' THEN t.quantity ELSE 0 END), 0)
    END AS avg_buy_price,
    SUM(CASE WHEN t.side='BUY' THEN t.quantity*t.price + t.fees
                                ELSE -(t.quantity*t.price) + t.fees END) AS cost_basis,
    MIN(t.trade_date) AS first_trade_date,
    MAX(t.trade_date) AS last_trade_date,
    COUNT(*)          AS trade_count
FROM trades t
GROUP BY t.portfolio_id, t.ticker
HAVING SUM(CASE WHEN t.side='BUY' THEN t.quantity ELSE -t.quantity END) <> 0;

CREATE OR REPLACE VIEW portfolio_summary AS
SELECT
    p.id              AS portfolio_id,
    p.user_id,
    p.name,
    p.base_currency,
    p.initial_cash,
    COUNT(DISTINCT t.id)        AS trade_count,
    COUNT(DISTINCT t.ticker)    AS ticker_count,
    COALESCE(SUM(CASE WHEN t.side='BUY'  THEN t.quantity*t.price + t.fees ELSE 0 END), 0) AS gross_invested,
    COALESCE(SUM(CASE WHEN t.side='SELL' THEN t.quantity*t.price - t.fees ELSE 0 END), 0) AS gross_proceeds
FROM portfolios p
LEFT JOIN trades t ON t.portfolio_id = p.id
GROUP BY p.id, p.user_id, p.name, p.base_currency, p.initial_cash;

-- =====================================================================
-- 13. updated_at triggers
-- =====================================================================
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER portfolios_touch   BEFORE UPDATE ON portfolios   FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER fundamentals_touch BEFORE UPDATE ON fundamentals FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER symbols_touch      BEFORE UPDATE ON symbols      FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER exchanges_touch    BEFORE UPDATE ON exchanges    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
