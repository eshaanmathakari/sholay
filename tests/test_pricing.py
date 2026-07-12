"""T2 — pricing: cost matches hand-computed value; cache is cheaper; unknown model fails."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricing

MODEL = "claude-sonnet-4-6"       # the agent's default model
MODEL_S5 = "claude-sonnet-5"      # available option (intro pricing through 2026-08-31)


def test_known_cost_matches_hand_computed():
    usage = {
        "input_tokens": 200_000,
        "output_tokens": 5_000,
        "cache_creation_input_tokens": 50_000,
        "cache_read_input_tokens": 1_000_000,
    }
    # (200000*3 + 5000*15 + 50000*3.75 + 1000000*0.30) / 1e6
    expected = (600_000 + 75_000 + 187_500 + 300_000) / 1_000_000  # 1.1625
    assert abs(pricing.cost_usd(usage, MODEL) - expected) < 1e-9


def test_sonnet5_intro_cost_matches_hand_computed():
    usage = {
        "input_tokens": 200_000,
        "output_tokens": 5_000,
        "cache_creation_input_tokens": 50_000,
        "cache_read_input_tokens": 1_000_000,
    }
    # (200000*2 + 5000*10 + 50000*2.50 + 1000000*0.20) / 1e6
    expected = (400_000 + 50_000 + 125_000 + 200_000) / 1_000_000  # 0.775
    assert abs(pricing.cost_usd(usage, MODEL_S5) - expected) < 1e-9


def test_both_agent_models_are_priced():
    # The agent's default model and the alternate priced option must both stay
    # in PRICES — cost_usd raises KeyError for an unpriced model.
    assert MODEL in pricing.PRICES
    assert MODEL_S5 in pricing.PRICES


def test_missing_fields_count_as_zero():
    assert pricing.cost_usd({"input_tokens": 1_000_000}, MODEL) == 3.00
    assert pricing.cost_usd({}, MODEL) == 0.0


def test_cache_read_is_cheaper_than_uncached():
    cached = {"input_tokens": 10_000, "output_tokens": 1_000, "cache_read_input_tokens": 500_000}
    # Same tokens, but the 500k cache-read tokens billed at full input price instead.
    uncached = {"input_tokens": 510_000, "output_tokens": 1_000}
    assert pricing.cost_usd(cached, MODEL) < pricing.cost_usd(uncached, MODEL)


def test_unknown_model_raises():
    try:
        pricing.cost_usd({"input_tokens": 1}, "no-such-model")
    except KeyError:
        return
    raise AssertionError("expected KeyError for an unpriced model")


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
