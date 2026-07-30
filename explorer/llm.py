"""LLM interface for result interpretation"""
import json
import os
import re
import pandas as pd
import ollama
from loguru import logger
import config  # noqa: F401 -- importing config loads .env into os.environ
from json_repair import repair_json

OLLAMA_HOST = os.getenv('OLLAMA_HOST', 'http://127.0.0.1:11434')
os.environ['OLLAMA_HOST'] = OLLAMA_HOST

# Schema-constrained output for interpret_results. Forces Ollama to emit
# exactly these keys/types as valid JSON -- no markdown fences, no preamble.
INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        "interpretation": {"type": "string"},
        "confidence": {"type": "number"},
        "tickers_of_interest": {"type": "array", "items": {"type": "string"}},
        "key_insight": {"type": "string"},
        "risk_factors": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string", "enum": ["BUY", "HOLD", "WATCH"]},
    },
    "required": [
        "interpretation", "confidence", "tickers_of_interest",
        "key_insight", "risk_factors", "recommended_action",
    ],
}


class LLMInterface:
    def __init__(self):
        self.exploration_model = os.getenv('EXPLORATION_MODEL', 'mistral:7b-instruct-v0.2-q8_0')
        self.analysis_model = os.getenv('ANALYSIS_MODEL', 'mixtral:8x7b-instruct-v0.1-q5_K_M')
        logger.info(f"LLM initialized: host={OLLAMA_HOST}, analysis={self.analysis_model}")

    def interpret_results(self, context: str, df: pd.DataFrame) -> dict:
        """Have the LLM analyze query results and return a structured dict."""
        fallback_action = "WATCH"
        if df.empty:
            return {
                "interpretation": "No results found",
                "confidence": 0.0,
                "tickers_of_interest": [],
                "key_insight": "",
                "risk_factors": [],
                "recommended_action": fallback_action,
            }

        tickers = df['ticker'].tolist()[:10] if 'ticker' in df.columns else []
        summary = f"Tickers: {', '.join(tickers)}\n"
        summary += f"Rows: {len(df)}, Columns: {list(df.columns)[:10]}\n"
        summary += f"Sample:\n{df.head(3).to_string()}\n"

        prompt = f"""Analyze these financial query results.

{context}

{summary[:1500]}

Return a JSON object with these fields:
- interpretation: at most 2 sentences, under 50 words
- confidence: number 0.0-1.0
- tickers_of_interest: up to 5 tickers
- key_insight: one short phrase
- risk_factors: up to 3 short phrases
- recommended_action: one of BUY, HOLD, WATCH"""

        raw = ""
        try:
            response = ollama.chat(
                model=self.exploration_model,
                messages=[{'role': 'user', 'content': prompt}],
                format=INTERPRET_SCHEMA,
                think=False,
                options={'temperature': 0.1, 'num_predict': 700},
            )
            raw = response['message']['content']
            return json.loads(repair_json(raw))
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed, using fallback: {e}, {response}")
            return {
                "interpretation": f"Found {len(df)} results",
                "confidence": 0.5,
                "tickers_of_interest": tickers[:5],
                "key_insight": "See raw data",
                "risk_factors": [],
                "recommended_action": fallback_action,
            }
        except Exception as e:
            logger.error(f"LLM failed: {e}")
            return {
                "interpretation": "LLM error",
                "confidence": 0.0,
                "tickers_of_interest": tickers[:5],
                "key_insight": "",
                "risk_factors": [],
                "recommended_action": fallback_action,
            }

    # ------------------------------------------------------------------ #
    # Grounded report synthesis
    # ------------------------------------------------------------------ #
    # GROUNDING CONTRACT: every company name, ticker, sector, and number in the
    # final report is TEMPLATED here from the `facts` list (built deterministically
    # from the DB in agent.build_facts). The LLM is called ONLY to write connective
    # commentary, and is explicitly forbidden from introducing any name or figure
    # not in the facts. The reader sees the templated picks first; the LLM prose
    # is sandboxed beneath them and can never rename a ticker or invent a price.

    @staticmethod
    def _money(v):
        if v is None:
            return None
        a = abs(v)
        for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if a >= div:
                return f"${v / div:.1f}{unit}"
        return f"${v:,.0f}"

    @staticmethod
    def _pct(v):
        if v is None:
            return None
        # EODHD ratio fields are stored as decimals (0.84 -> 84%). Treat |v|<=2 as
        # a fraction and scale; assume already-percent otherwise. Flip this rule if
        # your pipeline stores these fields already as percentages.
        pct = v * 100 if abs(v) <= 2 else v
        return f"{pct:.1f}%"

    def _render_picks(self, facts: list) -> str:
        """Deterministic, templated markdown for the conviction picks. No LLM."""
        blocks = []
        for i, f in enumerate(facts, 1):
            header = f"**{i}. {f['ticker']} — {f['name']}**"
            if f.get('sector'):
                header += f" · {f['sector']}"
            lines = [header, f"Flagged by: {', '.join(f.get('signals', [])) or '—'}"]

            metrics = []
            if f.get('price') is not None:
                metrics.append(f"Price ${f['price']:,.2f}")
            if f.get('market_cap') is not None:
                metrics.append(f"Mkt cap {self._money(f['market_cap'])}")
            if f.get('pe_ratio') is not None:
                metrics.append(f"P/E {f['pe_ratio']:.1f}")
            if f.get('return_on_equity') is not None:
                metrics.append(f"ROE {self._pct(f['return_on_equity'])}")
            if f.get('profit_margin') is not None:
                metrics.append(f"Margin {self._pct(f['profit_margin'])}")
            if f.get('dividend_yield'):
                metrics.append(f"Yield {self._pct(f['dividend_yield'])}")
            if f.get('rev_growth') is not None:
                metrics.append(f"Rev growth {self._pct(f['rev_growth'])}")
            if metrics:
                lines.append(" · ".join(metrics))

            blocks.append("  \n".join(lines))  # two-space hard breaks within a block
        return "\n\n".join(blocks)

    def _facts_for_prompt(self, facts: list) -> str:
        """Compact one-line-per-ticker fact sheet handed to the LLM as context."""
        lines = []
        for f in facts:
            parts = [f"{f['ticker']} = {f['name']}"]
            if f.get('sector'):
                parts.append(f"sector {f['sector']}")
            parts.append(f"signals: {', '.join(f.get('signals', []))}")
            if f.get('pe_ratio') is not None:
                parts.append(f"P/E {f['pe_ratio']:.1f}")
            if f.get('return_on_equity') is not None:
                parts.append(f"ROE {self._pct(f['return_on_equity'])}")
            if f.get('profit_margin') is not None:
                parts.append(f"margin {self._pct(f['profit_margin'])}")
            if f.get('dividend_yield'):
                parts.append(f"yield {self._pct(f['dividend_yield'])}")
            if f.get('price') is not None:
                parts.append(f"price ${f['price']:,.2f}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    def _commentary(self, facts_context: str, n_variations: int, universe_size) -> str:
        """LLM writes ONLY connective prose, constrained to the supplied facts."""
        universe = f"The database covers {universe_size} stocks. " if universe_size else ""
        prompt = f"""You are a portfolio strategist writing brief commentary for a daily research digest.

{universe}{n_variations} strategy variations ran overnight. Below are the FACTS for today's top conviction stocks. They have already been shown to the reader as a list above your commentary.

FACTS:
{facts_context}

Write short commentary with exactly these parts, as plain prose (no headers, no lists):
- Themes: 2-3 sentences on sector or factor patterns across these names.
- Risk to watch: 1 sentence.
- One idea: 1 sentence, framed as something to research further.

STRICT RULES -- these override any instinct to elaborate:
- Use ONLY the company names, tickers, sectors, and numbers in FACTS.
- Do NOT invent company names, price targets, dates, percentages, or any figure not in FACTS.
- Refer to each company by the EXACT name shown. Never guess a company's business from its ticker symbol.
- If you lack a fact, omit it rather than inventing it.
- Do not recommend a specific buy/sell price."""

        try:
            response = ollama.chat(
                model=self.analysis_model,
                messages=[{'role': 'user', 'content': prompt}],
                options={'temperature': 0.3, 'num_predict': 450},
            )
            return response['message']['content'].strip()
        except Exception as e:
            logger.error(f"Commentary generation failed: {e}")
            return "_Commentary unavailable for this run._"

    def synthesize_report(self, facts: list, all_findings: list, universe_size: int = None) -> str:
        """Assemble the final report: templated picks + sandboxed LLM commentary."""
        strategies_run = sorted(set(f.get('strategy', '') for f in all_findings))
        n_variations = len(all_findings)

        if not facts:
            # No multi-signal consensus -> deterministic note, never fabricated.
            return (
                "## Top Conviction Picks\n\n"
                "_No stocks were flagged by two or more strategies in this run._\n\n"
                f"Ran {n_variations} strategy variations across {len(strategies_run)} strategies."
            )

        picks_md = self._render_picks(facts)
        commentary = self._commentary(self._facts_for_prompt(facts), n_variations, universe_size)

        return (
            "## Top Conviction Picks\n\n"
            "_Ranked by the number of independent strategies flagging each name. "
            "Names and figures below are pulled directly from the database._\n\n"
            f"{picks_md}\n\n"
            "## Commentary\n\n"
            f"{commentary}\n"
        )
