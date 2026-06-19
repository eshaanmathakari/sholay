"""T4 — TradingView oracle: tolerance, structural checks, multi-source quote fetch,
and quote-failure → 'error'."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oracles import tradingview as tv

CATALOG = {"double top", "head and shoulders", "bull flag", "ascending triangle"}
CONFIG = {"tolerance": 0.005, "quote_symbol": "^spx"}
QUOTE = 5000.0


def _emit(**over):
    e = {
        "last_close": 5005.0,        # +0.1%, within 0.5%
        "tf_daily": True,
        "tf_4h": True,
        "patterns": [{"name": "Bull Flag", "timeframe": "4H", "rationale": "x"}],
        "paragraph": "Trend looks constructive on both timeframes.",
    }
    e.update(over)
    return e


def test_pass_within_tolerance():
    r = tv.evaluate(_emit(), CONFIG, QUOTE, CATALOG)
    assert r.status == "pass"
    assert r.fact_match is True


def test_fail_outside_tolerance():
    r = tv.evaluate(_emit(last_close=5200.0), CONFIG, QUOTE, CATALOG)   # +4%
    assert r.status == "fail"
    assert r.fact_match is False


def test_fail_missing_timeframe():
    r = tv.evaluate(_emit(tf_4h=False), CONFIG, QUOTE, CATALOG)
    assert r.status == "fail"


def test_fail_empty_paragraph():
    r = tv.evaluate(_emit(paragraph="  "), CONFIG, QUOTE, CATALOG)
    assert r.status == "fail"


def test_fail_pattern_not_in_catalog():
    r = tv.evaluate(_emit(patterns=[{"name": "Moon Cradle"}]), CONFIG, QUOTE, CATALOG)
    assert r.status == "fail"


def test_pass_with_one_valid_among_invalid():
    r = tv.evaluate(
        _emit(patterns=[{"name": "Moon Cradle"}, {"name": "Double Top"}]),
        CONFIG, QUOTE, CATALOG,
    )
    assert r.status == "pass"


def test_fail_no_patterns():
    r = tv.evaluate(_emit(patterns=[]), CONFIG, QUOTE, CATALOG)
    assert r.status == "fail"


def test_non_numeric_close_fails():
    r = tv.evaluate(_emit(last_close="5005"), CONFIG, QUOTE, CATALOG)
    assert r.status == "fail"
    assert r.fact_match is False


def test_quote_fetch_failure_is_error_not_fail():
    def boom(_sym):
        raise RuntimeError("network down")

    r = tv.run_oracle(_emit(), CONFIG, catalog=CATALOG, fetcher=boom)
    assert r.status == "error"
    assert r.fact_match is None


def test_run_oracle_passes_with_injected_fetcher():
    r = tv.run_oracle(_emit(), CONFIG, catalog=CATALOG, fetcher=lambda _sym: QUOTE)
    assert r.status == "pass"


def test_catalog_loads_from_shipped_file():
    catalog = tv.load_catalog(ROOT / "flows" / "ta_patterns.json")
    assert "double top" in catalog
    assert "head and shoulders" in catalog
    assert len(catalog) >= 15


# --- Multi-source quote fetch (Yahoo primary → stooq fallback) --------------------

def _yahoo_payload(price):
    return json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": price}}]}})


def test_yahoo_parses_regular_market_price():
    price = tv.fetch_yahoo("^GSPC", _get=lambda url, timeout, headers: _yahoo_payload(7526.5))
    assert price == 7526.5


def test_yahoo_encodes_symbol_and_sends_user_agent():
    seen = {}

    def fake_get(url, timeout, headers):
        seen["url"], seen["headers"] = url, headers
        return _yahoo_payload(7000.0)

    tv.fetch_yahoo("^GSPC", _get=fake_get)
    assert "%5EGSPC" in seen["url"]                       # ^ must be URL-encoded for Yahoo
    assert "User-Agent" in seen["headers"]                # Yahoo 403/404s without one


def test_yahoo_rejects_non_positive_price():
    try:
        tv.fetch_yahoo("^GSPC", _get=lambda url, timeout, headers: _yahoo_payload(0))
        assert False, "expected ValueError on non-positive price"
    except ValueError:
        pass


def test_stooq_parses_close():
    csv_text = "Symbol,Date,Time,Open,High,Low,Close,Volume\n^SPX,2026-06-16,22:00:00,7500,7540,7490,7531.25,0\n"
    price = tv.fetch_stooq("^spx", _get=lambda url, timeout, headers: csv_text)
    assert price == 7531.25


def test_cnbc_parses_last():
    payload = json.dumps({"FormattedQuoteResult": {"FormattedQuote": [{"symbol": ".SPX", "last": "7,526.50"}]}})
    price = tv.fetch_cnbc(".SPX", _get=lambda url, timeout, headers: payload)
    assert price == 7526.50


def test_stooq_daily_parses_last_row():
    csv_text = ("Date,Open,High,Low,Close,Volume\n"
                "2026-06-15,7400,7450,7390,7410.0,0\n"
                "2026-06-16,7500,7540,7490,7531.25,0\n")
    price = tv.fetch_stooq_daily("^spx", _get=lambda url, timeout, headers: csv_text)
    assert price == 7531.25


# A fake source-fetcher: each entry is a value to return or an Exception to raise, consumed
# in order. Accepts any args so it can stand in for fetch_yahoo/cnbc/stooq/stooq_daily.
def _scripted(*outcomes, log=None, tag="?"):
    seq = list(outcomes)

    def f(*_a, **_k):
        if log is not None:
            log.append(tag)
        out = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(out, Exception):
            raise out
        return out

    return f


def _patch_sources(**fns):
    """Swap module-level source fetchers; return a restore() callback."""
    saved = {name: getattr(tv, name) for name in fns}
    for name, fn in fns.items():
        setattr(tv, name, fn)
    return lambda: [setattr(tv, n, v) for n, v in saved.items()]


def test_multisource_prefers_yahoo():
    """Yahoo query1 succeeds → its price is returned; no later provider is reached."""
    log = []
    restore = _patch_sources(
        fetch_yahoo=_scripted(7526.0, log=log, tag="y"),
        fetch_cnbc=_scripted(8888.0, log=log, tag="c"),
        fetch_stooq=_scripted(9999.0, log=log, tag="s"),
        fetch_stooq_daily=_scripted(9999.0, log=log, tag="d"),
    )
    try:
        assert tv.fetch_spx_quote("^spx") == 7526.0
        assert log == ["y"]                       # query1 answered; query2/cnbc/stooq untouched
    finally:
        restore()


def test_multisource_falls_through_to_stooq():
    """Yahoo (both hosts) and CNBC fail → fetch_spx_quote falls through to stooq."""
    log = []
    restore = _patch_sources(
        fetch_yahoo=_scripted(RuntimeError("yahoo 403"), log=log, tag="y"),
        fetch_cnbc=_scripted(RuntimeError("cnbc 500"), log=log, tag="c"),
        fetch_stooq=_scripted(7500.0, log=log, tag="s"),
        fetch_stooq_daily=_scripted(9999.0, log=log, tag="d"),
    )
    try:
        assert tv.fetch_spx_quote("^spx") == 7500.0
        assert log == ["y", "y", "c", "s"]        # query1, query2, cnbc, then stooq answers
    finally:
        restore()


def test_transient_429_is_retried_then_succeeds():
    """A 429 on the first attempt is retried (backoff slept), second attempt succeeds."""
    class _HTTP429(Exception):
        code = 429

    slept = []
    saved_sleep = tv.time.sleep
    tv.time.sleep = lambda s: slept.append(s)
    restore = _patch_sources(fetch_yahoo=_scripted(_HTTP429(), 7526.0, tag="y"))
    try:
        assert tv.fetch_spx_quote("^spx") == 7526.0
        assert slept                              # backoff actually triggered on the 429
    finally:
        restore()
        tv.time.sleep = saved_sleep


def test_multisource_all_fail_raises_aggregated():
    """Every provider fails → RuntimeError naming the providers (run_oracle → 'error')."""
    restore = _patch_sources(
        fetch_yahoo=_scripted(RuntimeError("yahoo down")),
        fetch_cnbc=_scripted(RuntimeError("cnbc down")),
        fetch_stooq=_scripted(RuntimeError("stooq 404")),
        fetch_stooq_daily=_scripted(RuntimeError("stooq daily 404")),
    )
    try:
        try:
            tv.fetch_spx_quote("^spx")
            assert False, "expected RuntimeError when all sources fail"
        except RuntimeError as e:
            msg = str(e)
            assert "yahoo" in msg and "cnbc" in msg and "stooq" in msg
    finally:
        restore()


def test_run_oracle_error_when_all_sources_fail():
    """End-to-end: real fetch_spx_quote with every provider dead → oracle status 'error'."""
    restore = _patch_sources(
        fetch_yahoo=_scripted(RuntimeError("y")),
        fetch_cnbc=_scripted(RuntimeError("c")),
        fetch_stooq=_scripted(RuntimeError("s")),
        fetch_stooq_daily=_scripted(RuntimeError("d")),
    )
    try:
        r = tv.run_oracle(_emit(), CONFIG, catalog=CATALOG)
        assert r.status == "error"
        assert r.fact_match is None
    finally:
        restore()


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
