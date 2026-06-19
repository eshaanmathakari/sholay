"""Oracle for Flow #1 (TradingView).

Scores a run's emitted result objectively. PASS iff:
  - the emitted `last_close` is within tolerance of an independent quote (the only
    hard "did it read the chart correctly" check),
  - both timeframes were covered,
  - at least one flagged pattern's name is in the bundled catalog (structural
    validity — NOT whether the pattern is genuinely present), and
  - the paragraph is non-empty.

Whether a flagged pattern is *truly* on the chart, and prose quality, are subjective
and go to the human feedback step — never to this oracle.

`evaluate()` is pure (inject `quote` + `catalog`) so it's unit-testable without the
network. `run_oracle()` adds the live multi-source quote fetch (Yahoo → stooq) and the
catalog load, and turns a quote-fetch failure into status "error" (infra noise must not
count as an accuracy fail).
"""
import csv
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen

DEFAULT_TOLERANCE = 0.005

# Independent ground-truth quote sources for the S&P 500, tried in order by fetch_spx_quote.
# This is the oracle's *answer key* — it is NOT what the agent reads off the chart; it exists
# only to grade that reading. Multiple providers + retry so one flaky/blocked endpoint
# (Yahoo 429, stooq 404, …) doesn't strand a run with no way to verify.
YAHOO_URL = "https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
YAHOO_HOSTS = ("query1", "query2")     # query2 often answers when query1 rate-limits (429)
YAHOO_SYMBOL = "^GSPC"                  # S&P 500 cash index; URL-encoded to %5EGSPC at fetch time
CNBC_URL = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
            "?symbols={sym}&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json")
CNBC_SYMBOL = ".SPX"
STOOQ_LIGHT_URL = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/?s={sym}&i=d"
STOOQ_SYMBOL = "^spx"
# Several of these 403/404/429 without a browser-like User-Agent.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TRANSIENT_CODES = (429, 500, 502, 503, 504)


@dataclass
class OracleResult:
    status: str                      # "pass" | "fail" | "error"
    fact_match: Optional[bool]       # close within tolerance; None if not evaluated
    reasons: list                    # human-readable explanation of the verdict


def load_catalog(path) -> set:
    """Return the set of lowercased pattern names from a ta_patterns.json file."""
    data = json.loads(Path(path).read_text())
    return {p["name"].strip().lower() for p in data.get("patterns", [])}


def _pattern_names(emit: dict) -> list:
    out = []
    for p in emit.get("patterns") or []:
        name = p.get("name") if isinstance(p, dict) else p
        if isinstance(name, str) and name.strip():
            out.append(name.strip().lower())
    return out


def evaluate(emit: dict, config: dict, quote: float, catalog: set) -> OracleResult:
    """Pure scoring given an already-fetched quote and an already-loaded catalog."""
    reasons = []
    tolerance = config.get("tolerance", DEFAULT_TOLERANCE)

    # Objective fact: emitted close vs independent quote.
    fact_match = None
    close = emit.get("last_close")
    if not isinstance(close, (int, float)):
        reasons.append("last_close missing or not a number")
        fact_match = False
    elif quote in (None, 0):
        reasons.append("no valid independent quote to compare against")
        fact_match = False
    else:
        rel = abs(close - quote) / abs(quote)
        fact_match = rel <= tolerance
        reasons.append(
            f"close {close} vs quote {quote} → {rel:.4%} "
            f"({'within' if fact_match else 'outside'} {tolerance:.2%})"
        )

    # Structural checks.
    if not emit.get("tf_daily"):
        reasons.append("Daily timeframe not covered")
    if not emit.get("tf_4h"):
        reasons.append("4H timeframe not covered")

    names = _pattern_names(emit)
    known = [n for n in names if n in catalog]
    if not names:
        reasons.append("no patterns flagged")
    elif not known:
        reasons.append(f"none of the flagged patterns are in the catalog: {names}")

    paragraph = emit.get("paragraph")
    if not (isinstance(paragraph, str) and paragraph.strip()):
        reasons.append("paragraph missing or empty")

    passed = (
        fact_match is True
        and emit.get("tf_daily") and emit.get("tf_4h")
        and bool(known)
        and isinstance(paragraph, str) and bool(paragraph.strip())
    )
    return OracleResult("pass" if passed else "fail", fact_match, reasons)


def _http_get(url: str, timeout: int = 10, headers: Optional[dict] = None) -> str:
    """GET a URL and return the decoded body. Injectable in tests via the `_get` hooks below."""
    req = Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _positive(price: float, ctx: str) -> float:
    if price <= 0:
        raise ValueError(f"non-positive price from {ctx}: {price}")
    return price


def fetch_yahoo(symbol: str = YAHOO_SYMBOL, timeout: int = 10, host: str = "query1", *, _get=None) -> float:
    """Latest price from the Yahoo Finance chart API (chart.result[0].meta.regularMarketPrice)."""
    get = _get or _http_get
    url = YAHOO_URL.format(host=host, sym=urlquote(symbol, safe=""))   # ^GSPC -> %5EGSPC
    data = json.loads(get(url, timeout, {"User-Agent": USER_AGENT}))
    price = float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
    return _positive(price, f"yahoo:{host} {symbol}")


def fetch_cnbc(symbol: str = CNBC_SYMBOL, timeout: int = 10, *, _get=None) -> float:
    """Latest price from CNBC's public restQuote endpoint (no key)."""
    get = _get or _http_get
    data = json.loads(get(CNBC_URL.format(sym=urlquote(symbol, safe="")), timeout, {"User-Agent": USER_AGENT}))
    q = data["FormattedQuoteResult"]["FormattedQuote"][0]
    raw = q.get("last") or q.get("previousDayClosing") or q.get("previous_day_closing")
    return _positive(float(str(raw).replace(",", "")), f"cnbc {symbol}")


def fetch_stooq(symbol: str = STOOQ_SYMBOL, timeout: int = 10, *, _get=None) -> float:
    """Latest close from stooq's light CSV quote endpoint (no key)."""
    get = _get or _http_get
    text = get(STOOQ_LIGHT_URL.format(sym=urlquote(symbol, safe="")), timeout, {"User-Agent": USER_AGENT})
    row = next(csv.DictReader(io.StringIO(text)))
    return _positive(float(row["Close"]), f"stooq {symbol}: {row}")


def fetch_stooq_daily(symbol: str = STOOQ_SYMBOL, timeout: int = 10, *, _get=None) -> float:
    """Latest close from stooq's daily-history CSV (different path; dodges q/l/ 404s)."""
    get = _get or _http_get
    text = get(STOOQ_DAILY_URL.format(sym=urlquote(symbol, safe="")), timeout, {"User-Agent": USER_AGENT})
    rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("Close")]
    if not rows:
        raise ValueError(f"stooq daily returned no rows for {symbol}")
    return _positive(float(rows[-1]["Close"]), f"stooq:daily {symbol}")


def fetch_spx_quote(symbol: str = STOOQ_SYMBOL, timeout: int = 10, *, retries: int = 2, backoff: float = 1.5) -> float:
    """Latest S&P 500 quote, trying independent providers in order until one answers:
    Yahoo (query1 → query2), CNBC, stooq light CSV, stooq daily CSV. Transient errors
    (HTTP 429/5xx) are retried per source with linear backoff. Raises RuntimeError
    aggregating every source's final error if all fail — run_oracle turns that into status
    'error' so infra noise never scores as an accuracy fail. `symbol` is the stooq ticker;
    Yahoo/CNBC use their own module-level symbols.
    """
    sources = [(f"yahoo:{h}", (lambda h=h: fetch_yahoo(YAHOO_SYMBOL, timeout, h))) for h in YAHOO_HOSTS]
    sources += [
        ("cnbc", lambda: fetch_cnbc(CNBC_SYMBOL, timeout)),
        ("stooq", lambda: fetch_stooq(symbol, timeout)),
        ("stooq:daily", lambda: fetch_stooq_daily(symbol, timeout)),
    ]
    errors = []
    for label, fetch in sources:
        for attempt in range(retries + 1):
            try:
                return fetch()
            except Exception as e:
                if attempt < retries and getattr(e, "code", None) in TRANSIENT_CODES:
                    time.sleep(backoff * (attempt + 1))
                    continue
                errors.append(f"{label}: {e}")
                break
    raise RuntimeError("all quote sources failed — " + "; ".join(errors))


def run_oracle(emit: dict, config: dict, *, catalog: Optional[set] = None, fetcher=fetch_spx_quote) -> OracleResult:
    """Live entry point: fetch the quote, load the catalog, then evaluate.

    A quote-fetch failure yields status 'error' (not 'fail') so transient network
    issues don't pollute the accuracy numbers.
    """
    try:
        quote = fetcher(config.get("quote_symbol", "^spx"))
    except Exception as e:  # network / parse / value errors all mean "couldn't verify"
        return OracleResult("error", None, [f"quote fetch failed: {e}"])

    if catalog is None:
        ref = config.get("pattern_reference", "flows/ta_patterns.json")
        catalog = load_catalog(ref)
    return evaluate(emit, config, quote, catalog)
