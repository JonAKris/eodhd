--
-- PostgreSQL database dump
--

\restrict fSWdgu8UmvUgcTUPMfCVLfZjHqoqHa3xJrUYk3JKhNYjnGtpARHPVmXGo7QGUVY

-- Dumped from database version 16.14 (Ubuntu 16.14-1.pgdg24.04+1)
-- Dumped by pg_dump version 18.4 (Ubuntu 18.4-1.pgdg24.04+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: btree_gin; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA public;


--
-- Name: EXTENSION btree_gin; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION btree_gin IS 'support for indexing common datatypes in GIN';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: touch_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.touch_updated_at() OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: analyst_ratings_history; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.analyst_ratings_history (
    ticker text NOT NULL,
    date date NOT NULL,
    rating numeric(6,3),
    target_price numeric(18,4),
    strong_buy integer,
    buy integer,
    hold integer,
    sell integer,
    strong_sell integer
);


ALTER TABLE public.analyst_ratings_history OWNER TO postgres;

--
-- Name: balance_sheets; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.balance_sheets (
    ticker text NOT NULL,
    date date NOT NULL,
    period_type text NOT NULL,
    filing_date date,
    currency text,
    total_assets numeric(24,2),
    total_current_assets numeric(24,2),
    cash numeric(24,2),
    short_term_investments numeric(24,2),
    net_receivables numeric(24,2),
    inventory numeric(24,2),
    total_liab numeric(24,2),
    total_current_liabilities numeric(24,2),
    long_term_debt numeric(24,2),
    short_term_debt numeric(24,2),
    total_stockholder_equity numeric(24,2),
    retained_earnings numeric(24,2),
    common_stock numeric(24,2),
    raw jsonb
);


ALTER TABLE public.balance_sheets OWNER TO postgres;

--
-- Name: bond_fundamentals; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.bond_fundamentals (
    isin text NOT NULL,
    issuer text,
    currency text,
    coupon numeric(10,6),
    issue_date date,
    maturity_date date,
    face_value numeric(24,2),
    raw jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.bond_fundamentals OWNER TO postgres;

--
-- Name: cash_flow_statements; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.cash_flow_statements (
    ticker text NOT NULL,
    date date NOT NULL,
    period_type text NOT NULL,
    filing_date date,
    currency text,
    operating_cash_flow numeric(24,2),
    investing_cash_flow numeric(24,2),
    financing_cash_flow numeric(24,2),
    capital_expenditures numeric(24,2),
    free_cash_flow numeric(24,2),
    dividends_paid numeric(24,2),
    stock_repurchase numeric(24,2),
    change_in_cash numeric(24,2),
    raw jsonb
);


ALTER TABLE public.cash_flow_statements OWNER TO postgres;

--
-- Name: dividends; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.dividends (
    ticker text NOT NULL,
    ex_date date NOT NULL,
    declaration_date date,
    record_date date,
    payment_date date,
    period text,
    value numeric(24,6),
    unadjusted_value numeric(24,6),
    currency text
);


ALTER TABLE public.dividends OWNER TO postgres;

--
-- Name: earnings_calendar; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.earnings_calendar (
    ticker text NOT NULL,
    report_date date NOT NULL,
    date date,
    before_after_market text,
    currency text,
    eps_actual numeric(24,6),
    eps_estimate numeric(24,6),
    eps_difference numeric(24,6),
    surprise_pct numeric(24,6)
);


ALTER TABLE public.earnings_calendar OWNER TO postgres;

--
-- Name: earnings_history; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.earnings_history (
    ticker text NOT NULL,
    report_date date NOT NULL,
    date date,
    before_after_market text,
    currency text,
    eps_actual numeric(24,6),
    eps_estimate numeric(24,6),
    eps_difference numeric(24,6),
    surprise_pct numeric(24,6)
);


ALTER TABLE public.earnings_history OWNER TO postgres;

--
-- Name: earnings_trend; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.earnings_trend (
    ticker text NOT NULL,
    date date NOT NULL,
    period text NOT NULL,
    growth numeric(24,6),
    earnings_estimate_avg numeric(24,6),
    earnings_estimate_low numeric(24,6),
    earnings_estimate_high numeric(24,6),
    revenue_estimate_avg numeric(24,2),
    revenue_estimate_low numeric(24,2),
    revenue_estimate_high numeric(24,2)
);


ALTER TABLE public.earnings_trend OWNER TO postgres;

--
-- Name: economic_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.economic_events (
    id bigint NOT NULL,
    event_date timestamp with time zone NOT NULL,
    country text,
    type text,
    comparison text,
    period text,
    actual numeric(24,6),
    previous numeric(24,6),
    estimate numeric(24,6),
    change numeric(24,6),
    change_pct numeric(10,6)
);


ALTER TABLE public.economic_events OWNER TO postgres;

--
-- Name: economic_events_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.economic_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.economic_events_id_seq OWNER TO postgres;

--
-- Name: economic_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.economic_events_id_seq OWNED BY public.economic_events.id;


--
-- Name: eod_prices; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.eod_prices (
    ticker text NOT NULL,
    date date NOT NULL,
    open numeric(24,6),
    high numeric(24,6),
    low numeric(24,6),
    close numeric(24,6),
    adjusted_close numeric(24,6),
    volume bigint
);


ALTER TABLE public.eod_prices OWNER TO postgres;

--
-- Name: exchange_details; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.exchange_details (
    exchange_code text NOT NULL,
    timezone text,
    trading_hours jsonb,
    holidays jsonb,
    raw jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.exchange_details OWNER TO postgres;

--
-- Name: exchanges; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.exchanges (
    code text NOT NULL,
    name text NOT NULL,
    operating_mic text,
    country text,
    currency text,
    country_iso2 text,
    raw jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.exchanges OWNER TO postgres;

--
-- Name: fund_holders; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.fund_holders (
    ticker text NOT NULL,
    holder_name text NOT NULL,
    report_date date NOT NULL,
    pct_shares numeric(10,6),
    shares_held numeric(20,2)
);


ALTER TABLE public.fund_holders OWNER TO postgres;

--
-- Name: fundamentals; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.fundamentals (
    ticker text NOT NULL,
    asset_type text,
    name text,
    description text,
    sector text,
    industry text,
    gic_sector text,
    gic_industry text,
    country text,
    country_iso text,
    currency text,
    web_url text,
    logo_url text,
    full_time_employees integer,
    ipo_date date,
    fiscal_year_end text,
    cik text,
    isin text,
    primary_ticker text,
    is_delisted boolean,
    market_cap numeric(24,2),
    ebitda numeric(24,2),
    pe_ratio numeric(18,4),
    peg_ratio numeric(18,4),
    eps numeric(18,4),
    book_value numeric(18,4),
    dividend_share numeric(24,6),
    dividend_yield numeric(24,6),
    profit_margin numeric(24,6),
    operating_margin numeric(24,6),
    return_on_assets numeric(24,6),
    return_on_equity numeric(24,6),
    revenue_ttm numeric(24,2),
    gross_profit_ttm numeric(24,2),
    quarterly_revenue_growth numeric(24,6),
    quarterly_earnings_growth numeric(24,6),
    wall_street_target_price numeric(18,4),
    general jsonb,
    highlights jsonb,
    valuation jsonb,
    shares_stats jsonb,
    technicals jsonb,
    splits_dividends jsonb,
    analyst_ratings jsonb,
    holders jsonb,
    insider_transactions jsonb,
    esg_scores jsonb,
    outstanding_shares jsonb,
    earnings jsonb,
    financials jsonb,
    etf_data jsonb,
    components jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.fundamentals OWNER TO postgres;

--
-- Name: historical_market_cap; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.historical_market_cap (
    ticker text NOT NULL,
    date date NOT NULL,
    market_cap numeric(24,2)
);


ALTER TABLE public.historical_market_cap OWNER TO postgres;

--
-- Name: income_statements; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.income_statements (
    ticker text NOT NULL,
    date date NOT NULL,
    period_type text NOT NULL,
    filing_date date,
    currency text,
    total_revenue numeric(24,2),
    cost_of_revenue numeric(24,2),
    gross_profit numeric(24,2),
    research_development numeric(24,2),
    selling_general_admin numeric(24,2),
    total_operating_expenses numeric(24,2),
    operating_income numeric(24,2),
    interest_expense numeric(24,2),
    income_before_tax numeric(24,2),
    income_tax_expense numeric(24,2),
    net_income numeric(24,2),
    ebit numeric(24,2),
    ebitda numeric(24,2),
    raw jsonb
);


ALTER TABLE public.income_statements OWNER TO postgres;

--
-- Name: index_constituents; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.index_constituents (
    index_code text NOT NULL,
    member_code text NOT NULL,
    member_exchange text,
    member_name text,
    sector text,
    industry text,
    weight numeric(10,6),
    as_of_date date NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


ALTER TABLE public.index_constituents OWNER TO postgres;

--
-- Name: ingest_log; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ingest_log (
    id bigint NOT NULL,
    endpoint text NOT NULL,
    ticker text,
    params jsonb,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text DEFAULT 'started'::text NOT NULL,
    rows_written integer,
    error_message text
);


ALTER TABLE public.ingest_log OWNER TO postgres;

--
-- Name: ingest_log_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.ingest_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.ingest_log_id_seq OWNER TO postgres;

--
-- Name: ingest_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.ingest_log_id_seq OWNED BY public.ingest_log.id;


--
-- Name: insider_transactions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.insider_transactions (
    ticker text NOT NULL,
    transaction_date date NOT NULL,
    owner_cik text,
    owner_name text NOT NULL,
    relationship text,
    transaction_code text NOT NULL,
    acquisition_or_disposition text,
    shares numeric(20,2) NOT NULL,
    price numeric(18,4),
    value numeric(24,2)
);


ALTER TABLE public.insider_transactions OWNER TO postgres;

--
-- Name: institutional_holders; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.institutional_holders (
    ticker text NOT NULL,
    holder_name text NOT NULL,
    report_date date NOT NULL,
    pct_shares numeric(10,6),
    pct_assets numeric(10,6),
    shares_held numeric(20,2)
);


ALTER TABLE public.institutional_holders OWNER TO postgres;

--
-- Name: intraday_prices; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.intraday_prices (
    ticker text NOT NULL,
    ts timestamp with time zone NOT NULL,
    "interval" text NOT NULL,
    open numeric(24,6),
    high numeric(24,6),
    low numeric(24,6),
    close numeric(24,6),
    volume bigint
);


ALTER TABLE public.intraday_prices OWNER TO postgres;

--
-- Name: ipo_calendar; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ipo_calendar (
    code text NOT NULL,
    exchange text NOT NULL,
    name text,
    currency text,
    start_date date NOT NULL,
    filing_date date,
    amended_date date,
    price_from numeric(18,4),
    price_to numeric(18,4),
    offer_price numeric(18,4),
    shares numeric(20,2),
    deal_type text
);


ALTER TABLE public.ipo_calendar OWNER TO postgres;

--
-- Name: macro_indicators; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.macro_indicators (
    country text NOT NULL,
    indicator text NOT NULL,
    date date NOT NULL,
    value numeric(24,6)
);


ALTER TABLE public.macro_indicators OWNER TO postgres;

--
-- Name: news; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.news (
    id bigint NOT NULL,
    eodhd_uuid text,
    ticker text,
    published_at timestamp with time zone NOT NULL,
    title text NOT NULL,
    content text,
    link text,
    symbols text[],
    tags text[],
    sentiment_polarity numeric(10,6),
    sentiment_neg numeric(10,6),
    sentiment_neu numeric(10,6),
    sentiment_pos numeric(10,6)
);


ALTER TABLE public.news OWNER TO postgres;

--
-- Name: news_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.news_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.news_id_seq OWNER TO postgres;

--
-- Name: news_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.news_id_seq OWNED BY public.news.id;


--
-- Name: options_chains; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.options_chains (
    id bigint NOT NULL,
    ticker text NOT NULL,
    expiration_date date NOT NULL,
    option_type text NOT NULL,
    strike numeric(18,4) NOT NULL,
    last_trade_date timestamp with time zone,
    last numeric(18,4),
    change numeric(18,4),
    change_pct numeric(10,4),
    bid numeric(18,4),
    ask numeric(18,4),
    volume bigint,
    open_interest bigint,
    implied_volatility numeric(24,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    rho numeric(10,6),
    theoretical numeric(18,4),
    intrinsic_value numeric(18,4),
    time_value numeric(18,4),
    in_the_money boolean,
    snapshot_date date NOT NULL
);


ALTER TABLE public.options_chains OWNER TO postgres;

--
-- Name: options_chains_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.options_chains_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.options_chains_id_seq OWNER TO postgres;

--
-- Name: options_chains_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.options_chains_id_seq OWNED BY public.options_chains.id;


--
-- Name: trades; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.trades (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    portfolio_id uuid NOT NULL,
    ticker text NOT NULL,
    side text NOT NULL,
    trade_date date NOT NULL,
    quantity numeric(20,8) NOT NULL,
    price numeric(20,8) NOT NULL,
    fees numeric(20,4) DEFAULT 0 NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT trades_price_check CHECK ((price >= (0)::numeric)),
    CONSTRAINT trades_quantity_check CHECK ((quantity > (0)::numeric)),
    CONSTRAINT trades_side_check CHECK ((side = ANY (ARRAY['BUY'::text, 'SELL'::text])))
);


ALTER TABLE public.trades OWNER TO postgres;

--
-- Name: portfolio_positions; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.portfolio_positions AS
 SELECT portfolio_id,
    ticker,
    sum(
        CASE
            WHEN (side = 'BUY'::text) THEN quantity
            ELSE (- quantity)
        END) AS quantity,
        CASE
            WHEN (sum(
            CASE
                WHEN (side = 'BUY'::text) THEN quantity
                ELSE (0)::numeric
            END) = (0)::numeric) THEN (0)::numeric
            ELSE (sum(
            CASE
                WHEN (side = 'BUY'::text) THEN (quantity * price)
                ELSE (0)::numeric
            END) / NULLIF(sum(
            CASE
                WHEN (side = 'BUY'::text) THEN quantity
                ELSE (0)::numeric
            END), (0)::numeric))
        END AS avg_buy_price,
    sum(
        CASE
            WHEN (side = 'BUY'::text) THEN ((quantity * price) + fees)
            ELSE ((- (quantity * price)) + fees)
        END) AS cost_basis,
    min(trade_date) AS first_trade_date,
    max(trade_date) AS last_trade_date,
    count(*) AS trade_count
   FROM public.trades t
  GROUP BY portfolio_id, ticker
 HAVING (sum(
        CASE
            WHEN (side = 'BUY'::text) THEN quantity
            ELSE (- quantity)
        END) <> (0)::numeric);


ALTER VIEW public.portfolio_positions OWNER TO postgres;

--
-- Name: portfolios; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.portfolios (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    base_currency text DEFAULT 'USD'::text NOT NULL,
    initial_cash numeric(20,2) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.portfolios OWNER TO postgres;

--
-- Name: portfolio_summary; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW public.portfolio_summary AS
 SELECT p.id AS portfolio_id,
    p.user_id,
    p.name,
    p.base_currency,
    p.initial_cash,
    count(DISTINCT t.id) AS trade_count,
    count(DISTINCT t.ticker) AS ticker_count,
    COALESCE(sum(
        CASE
            WHEN (t.side = 'BUY'::text) THEN ((t.quantity * t.price) + t.fees)
            ELSE (0)::numeric
        END), (0)::numeric) AS gross_invested,
    COALESCE(sum(
        CASE
            WHEN (t.side = 'SELL'::text) THEN ((t.quantity * t.price) - t.fees)
            ELSE (0)::numeric
        END), (0)::numeric) AS gross_proceeds
   FROM (public.portfolios p
     LEFT JOIN public.trades t ON ((t.portfolio_id = p.id)))
  GROUP BY p.id, p.user_id, p.name, p.base_currency, p.initial_cash;


ALTER VIEW public.portfolio_summary OWNER TO postgres;

--
-- Name: realtime_quotes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.realtime_quotes (
    ticker text NOT NULL,
    ts timestamp with time zone NOT NULL,
    open numeric(24,6),
    high numeric(24,6),
    low numeric(24,6),
    close numeric(24,6),
    previous_close numeric(24,6),
    change numeric(24,6),
    change_pct numeric(10,4),
    volume bigint,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.realtime_quotes OWNER TO postgres;

--
-- Name: sentiment_daily; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.sentiment_daily (
    ticker text NOT NULL,
    date date NOT NULL,
    count integer,
    normalized numeric(10,6)
);


ALTER TABLE public.sentiment_daily OWNER TO postgres;

--
-- Name: shares_outstanding; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.shares_outstanding (
    ticker text NOT NULL,
    date date NOT NULL,
    frequency text NOT NULL,
    shares numeric(20,2)
);


ALTER TABLE public.shares_outstanding OWNER TO postgres;

--
-- Name: splits; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.splits (
    ticker text NOT NULL,
    date date NOT NULL,
    split_text text,
    ratio_numer numeric(24,6),
    ratio_denom numeric(24,6)
);


ALTER TABLE public.splits OWNER TO postgres;

--
-- Name: splits_calendar; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.splits_calendar (
    code text NOT NULL,
    exchange text NOT NULL,
    name text,
    split_date date NOT NULL,
    optionable boolean,
    old_shares numeric(20,2),
    new_shares numeric(20,2),
    split_from numeric(24,6),
    split_to numeric(24,6)
);


ALTER TABLE public.splits_calendar OWNER TO postgres;

--
-- Name: symbol_change_history; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.symbol_change_history (
    id bigint NOT NULL,
    date date NOT NULL,
    old_symbol text NOT NULL,
    new_symbol text NOT NULL,
    name text
);


ALTER TABLE public.symbol_change_history OWNER TO postgres;

--
-- Name: symbol_change_history_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.symbol_change_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.symbol_change_history_id_seq OWNER TO postgres;

--
-- Name: symbol_change_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.symbol_change_history_id_seq OWNED BY public.symbol_change_history.id;


--
-- Name: symbols; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.symbols (
    ticker text NOT NULL,
    code text NOT NULL,
    exchange_code text NOT NULL,
    name text,
    country text,
    currency text,
    type text,
    isin text,
    is_active boolean DEFAULT true NOT NULL,
    delisted_on date,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.symbols OWNER TO postgres;

--
-- Name: technical_indicators; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.technical_indicators (
    ticker text NOT NULL,
    function text NOT NULL,
    period integer NOT NULL,
    date date NOT NULL,
    value numeric(20,8),
    extra jsonb
);


ALTER TABLE public.technical_indicators OWNER TO postgres;

--
-- Name: tick_data; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tick_data (
    id bigint NOT NULL,
    ticker text NOT NULL,
    ts timestamp with time zone NOT NULL,
    price numeric(24,6),
    size bigint,
    side text
);


ALTER TABLE public.tick_data OWNER TO postgres;

--
-- Name: tick_data_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.tick_data_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tick_data_id_seq OWNER TO postgres;

--
-- Name: tick_data_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.tick_data_id_seq OWNED BY public.tick_data.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email text NOT NULL,
    display_name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.users OWNER TO postgres;

--
-- Name: economic_events id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.economic_events ALTER COLUMN id SET DEFAULT nextval('public.economic_events_id_seq'::regclass);


--
-- Name: ingest_log id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ingest_log ALTER COLUMN id SET DEFAULT nextval('public.ingest_log_id_seq'::regclass);


--
-- Name: news id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.news ALTER COLUMN id SET DEFAULT nextval('public.news_id_seq'::regclass);


--
-- Name: options_chains id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.options_chains ALTER COLUMN id SET DEFAULT nextval('public.options_chains_id_seq'::regclass);


--
-- Name: symbol_change_history id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.symbol_change_history ALTER COLUMN id SET DEFAULT nextval('public.symbol_change_history_id_seq'::regclass);


--
-- Name: tick_data id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tick_data ALTER COLUMN id SET DEFAULT nextval('public.tick_data_id_seq'::regclass);


--
-- Name: analyst_ratings_history analyst_ratings_history_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analyst_ratings_history
    ADD CONSTRAINT analyst_ratings_history_pkey PRIMARY KEY (ticker, date);


--
-- Name: balance_sheets balance_sheets_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.balance_sheets
    ADD CONSTRAINT balance_sheets_pkey PRIMARY KEY (ticker, date, period_type);


--
-- Name: bond_fundamentals bond_fundamentals_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bond_fundamentals
    ADD CONSTRAINT bond_fundamentals_pkey PRIMARY KEY (isin);


--
-- Name: cash_flow_statements cash_flow_statements_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cash_flow_statements
    ADD CONSTRAINT cash_flow_statements_pkey PRIMARY KEY (ticker, date, period_type);


--
-- Name: dividends dividends_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.dividends
    ADD CONSTRAINT dividends_pkey PRIMARY KEY (ticker, ex_date);


--
-- Name: earnings_calendar earnings_calendar_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.earnings_calendar
    ADD CONSTRAINT earnings_calendar_pkey PRIMARY KEY (ticker, report_date);


--
-- Name: earnings_history earnings_history_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.earnings_history
    ADD CONSTRAINT earnings_history_pkey PRIMARY KEY (ticker, report_date);


--
-- Name: earnings_trend earnings_trend_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.earnings_trend
    ADD CONSTRAINT earnings_trend_pkey PRIMARY KEY (ticker, date, period);


--
-- Name: economic_events economic_events_event_date_country_type_period_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.economic_events
    ADD CONSTRAINT economic_events_event_date_country_type_period_key UNIQUE (event_date, country, type, period);


--
-- Name: economic_events economic_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.economic_events
    ADD CONSTRAINT economic_events_pkey PRIMARY KEY (id);


--
-- Name: eod_prices eod_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.eod_prices
    ADD CONSTRAINT eod_prices_pkey PRIMARY KEY (ticker, date);


--
-- Name: exchange_details exchange_details_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.exchange_details
    ADD CONSTRAINT exchange_details_pkey PRIMARY KEY (exchange_code);


--
-- Name: exchanges exchanges_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.exchanges
    ADD CONSTRAINT exchanges_pkey PRIMARY KEY (code);


--
-- Name: fund_holders fund_holders_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.fund_holders
    ADD CONSTRAINT fund_holders_pkey PRIMARY KEY (ticker, holder_name, report_date);


--
-- Name: fundamentals fundamentals_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.fundamentals
    ADD CONSTRAINT fundamentals_pkey PRIMARY KEY (ticker);


--
-- Name: historical_market_cap historical_market_cap_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.historical_market_cap
    ADD CONSTRAINT historical_market_cap_pkey PRIMARY KEY (ticker, date);


--
-- Name: income_statements income_statements_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.income_statements
    ADD CONSTRAINT income_statements_pkey PRIMARY KEY (ticker, date, period_type);


--
-- Name: index_constituents index_constituents_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.index_constituents
    ADD CONSTRAINT index_constituents_pkey PRIMARY KEY (index_code, member_code, as_of_date);


--
-- Name: ingest_log ingest_log_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ingest_log
    ADD CONSTRAINT ingest_log_pkey PRIMARY KEY (id);


--
-- Name: insider_transactions insider_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.insider_transactions
    ADD CONSTRAINT insider_transactions_pkey PRIMARY KEY (ticker, transaction_date, owner_name, transaction_code, shares);


--
-- Name: institutional_holders institutional_holders_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.institutional_holders
    ADD CONSTRAINT institutional_holders_pkey PRIMARY KEY (ticker, holder_name, report_date);


--
-- Name: intraday_prices intraday_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.intraday_prices
    ADD CONSTRAINT intraday_prices_pkey PRIMARY KEY (ticker, ts, "interval");


--
-- Name: ipo_calendar ipo_calendar_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ipo_calendar
    ADD CONSTRAINT ipo_calendar_pkey PRIMARY KEY (code, exchange, start_date);


--
-- Name: macro_indicators macro_indicators_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.macro_indicators
    ADD CONSTRAINT macro_indicators_pkey PRIMARY KEY (country, indicator, date);


--
-- Name: news news_eodhd_uuid_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.news
    ADD CONSTRAINT news_eodhd_uuid_key UNIQUE (eodhd_uuid);


--
-- Name: news news_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.news
    ADD CONSTRAINT news_pkey PRIMARY KEY (id);


--
-- Name: options_chains options_chains_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.options_chains
    ADD CONSTRAINT options_chains_pkey PRIMARY KEY (id);


--
-- Name: options_chains options_chains_ticker_expiration_date_option_type_strike_sn_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.options_chains
    ADD CONSTRAINT options_chains_ticker_expiration_date_option_type_strike_sn_key UNIQUE (ticker, expiration_date, option_type, strike, snapshot_date);


--
-- Name: portfolios portfolios_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.portfolios
    ADD CONSTRAINT portfolios_pkey PRIMARY KEY (id);


--
-- Name: portfolios portfolios_user_id_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.portfolios
    ADD CONSTRAINT portfolios_user_id_name_key UNIQUE (user_id, name);


--
-- Name: realtime_quotes realtime_quotes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.realtime_quotes
    ADD CONSTRAINT realtime_quotes_pkey PRIMARY KEY (ticker);


--
-- Name: sentiment_daily sentiment_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sentiment_daily
    ADD CONSTRAINT sentiment_daily_pkey PRIMARY KEY (ticker, date);


--
-- Name: shares_outstanding shares_outstanding_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shares_outstanding
    ADD CONSTRAINT shares_outstanding_pkey PRIMARY KEY (ticker, date, frequency);


--
-- Name: splits_calendar splits_calendar_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.splits_calendar
    ADD CONSTRAINT splits_calendar_pkey PRIMARY KEY (code, exchange, split_date);


--
-- Name: splits splits_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.splits
    ADD CONSTRAINT splits_pkey PRIMARY KEY (ticker, date);


--
-- Name: symbol_change_history symbol_change_history_date_old_symbol_new_symbol_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.symbol_change_history
    ADD CONSTRAINT symbol_change_history_date_old_symbol_new_symbol_key UNIQUE (date, old_symbol, new_symbol);


--
-- Name: symbol_change_history symbol_change_history_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.symbol_change_history
    ADD CONSTRAINT symbol_change_history_pkey PRIMARY KEY (id);


--
-- Name: symbols symbols_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.symbols
    ADD CONSTRAINT symbols_pkey PRIMARY KEY (ticker);


--
-- Name: technical_indicators technical_indicators_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.technical_indicators
    ADD CONSTRAINT technical_indicators_pkey PRIMARY KEY (ticker, function, period, date);


--
-- Name: tick_data tick_data_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tick_data
    ADD CONSTRAINT tick_data_pkey PRIMARY KEY (id);


--
-- Name: trades trades_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_pkey PRIMARY KEY (id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: economic_events_country_date_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX economic_events_country_date_idx ON public.economic_events USING btree (country, event_date DESC);


--
-- Name: eod_prices_date_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX eod_prices_date_idx ON public.eod_prices USING btree (date);


--
-- Name: fundamentals_country_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX fundamentals_country_idx ON public.fundamentals USING btree (country);


--
-- Name: fundamentals_financials_gin; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX fundamentals_financials_gin ON public.fundamentals USING gin (financials);


--
-- Name: fundamentals_general_gin; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX fundamentals_general_gin ON public.fundamentals USING gin (general);


--
-- Name: fundamentals_industry_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX fundamentals_industry_idx ON public.fundamentals USING btree (industry);


--
-- Name: fundamentals_sector_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX fundamentals_sector_idx ON public.fundamentals USING btree (sector);


--
-- Name: ingest_log_endpoint_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ingest_log_endpoint_idx ON public.ingest_log USING btree (endpoint, started_at DESC);


--
-- Name: intraday_prices_ts_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX intraday_prices_ts_idx ON public.intraday_prices USING btree (ts);


--
-- Name: news_symbols_gin; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX news_symbols_gin ON public.news USING gin (symbols);


--
-- Name: news_tags_gin; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX news_tags_gin ON public.news USING gin (tags);


--
-- Name: news_ticker_ts_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX news_ticker_ts_idx ON public.news USING btree (ticker, published_at DESC);


--
-- Name: options_chains_ticker_exp_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX options_chains_ticker_exp_idx ON public.options_chains USING btree (ticker, expiration_date);


--
-- Name: symbols_code_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX symbols_code_idx ON public.symbols USING btree (code);


--
-- Name: symbols_exchange_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX symbols_exchange_idx ON public.symbols USING btree (exchange_code);


--
-- Name: symbols_isin_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX symbols_isin_idx ON public.symbols USING btree (isin) WHERE (isin IS NOT NULL);


--
-- Name: tick_data_ticker_ts_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX tick_data_ticker_ts_idx ON public.tick_data USING btree (ticker, ts);


--
-- Name: trades_date_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX trades_date_idx ON public.trades USING btree (trade_date);


--
-- Name: trades_portfolio_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX trades_portfolio_idx ON public.trades USING btree (portfolio_id);


--
-- Name: trades_ticker_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX trades_ticker_idx ON public.trades USING btree (ticker);


--
-- Name: exchanges exchanges_touch; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER exchanges_touch BEFORE UPDATE ON public.exchanges FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();


--
-- Name: fundamentals fundamentals_touch; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER fundamentals_touch BEFORE UPDATE ON public.fundamentals FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();


--
-- Name: portfolios portfolios_touch; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER portfolios_touch BEFORE UPDATE ON public.portfolios FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();


--
-- Name: symbols symbols_touch; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER symbols_touch BEFORE UPDATE ON public.symbols FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();


--
-- Name: analyst_ratings_history analyst_ratings_history_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.analyst_ratings_history
    ADD CONSTRAINT analyst_ratings_history_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: balance_sheets balance_sheets_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.balance_sheets
    ADD CONSTRAINT balance_sheets_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: cash_flow_statements cash_flow_statements_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cash_flow_statements
    ADD CONSTRAINT cash_flow_statements_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: dividends dividends_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.dividends
    ADD CONSTRAINT dividends_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: earnings_history earnings_history_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.earnings_history
    ADD CONSTRAINT earnings_history_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: earnings_trend earnings_trend_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.earnings_trend
    ADD CONSTRAINT earnings_trend_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: eod_prices eod_prices_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.eod_prices
    ADD CONSTRAINT eod_prices_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: exchange_details exchange_details_exchange_code_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.exchange_details
    ADD CONSTRAINT exchange_details_exchange_code_fkey FOREIGN KEY (exchange_code) REFERENCES public.exchanges(code) ON DELETE CASCADE;


--
-- Name: fund_holders fund_holders_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.fund_holders
    ADD CONSTRAINT fund_holders_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: fundamentals fundamentals_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.fundamentals
    ADD CONSTRAINT fundamentals_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: historical_market_cap historical_market_cap_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.historical_market_cap
    ADD CONSTRAINT historical_market_cap_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: income_statements income_statements_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.income_statements
    ADD CONSTRAINT income_statements_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: insider_transactions insider_transactions_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.insider_transactions
    ADD CONSTRAINT insider_transactions_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: institutional_holders institutional_holders_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.institutional_holders
    ADD CONSTRAINT institutional_holders_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: intraday_prices intraday_prices_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.intraday_prices
    ADD CONSTRAINT intraday_prices_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: news news_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.news
    ADD CONSTRAINT news_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE SET NULL;


--
-- Name: options_chains options_chains_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.options_chains
    ADD CONSTRAINT options_chains_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: portfolios portfolios_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.portfolios
    ADD CONSTRAINT portfolios_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: realtime_quotes realtime_quotes_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.realtime_quotes
    ADD CONSTRAINT realtime_quotes_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: sentiment_daily sentiment_daily_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sentiment_daily
    ADD CONSTRAINT sentiment_daily_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: shares_outstanding shares_outstanding_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shares_outstanding
    ADD CONSTRAINT shares_outstanding_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: splits splits_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.splits
    ADD CONSTRAINT splits_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: symbols symbols_exchange_code_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.symbols
    ADD CONSTRAINT symbols_exchange_code_fkey FOREIGN KEY (exchange_code) REFERENCES public.exchanges(code) ON DELETE CASCADE;


--
-- Name: technical_indicators technical_indicators_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.technical_indicators
    ADD CONSTRAINT technical_indicators_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: tick_data tick_data_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tick_data
    ADD CONSTRAINT tick_data_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker) ON DELETE CASCADE;


--
-- Name: trades trades_portfolio_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_portfolio_id_fkey FOREIGN KEY (portfolio_id) REFERENCES public.portfolios(id) ON DELETE CASCADE;


--
-- Name: trades trades_ticker_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_ticker_fkey FOREIGN KEY (ticker) REFERENCES public.symbols(ticker);


--
-- PostgreSQL database dump complete
--

\unrestrict fSWdgu8UmvUgcTUPMfCVLfZjHqoqHa3xJrUYk3JKhNYjnGtpARHPVmXGo7QGUVY

