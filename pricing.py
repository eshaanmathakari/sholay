"""Verified per-model token pricing and per-run cost computation.

Prices are USD per 1,000,000 tokens, taken from the Claude API pricing reference.
The agent's default model is `claude-sonnet-4-6`; a verified `claude-sonnet-5` entry
is kept alongside it (introductory rates through 2026-08-31) so switching the default
is a one-line change that reprices correctly. Cost is computed from the REAL
measured token counts the agent records each run (input / output / cache read /
cache write), so there is no estimation here — just a lookup and a multiply.
Screenshot/image tokens are already inside the API-reported input counts.

Update PRICES when the model or the rates change; that single edit reprices every
historical row when `report.py` recomputes, and every future run.
"""

# USD per 1,000,000 tokens. Keys match the agent's `token_usage` field names
# (see `_USAGE_FIELDS` in agent.py) so a usage dict maps straight onto rates.
# Cache write is the 5-minute ephemeral TTL the agent uses (1.25x input); a 1h
# TTL would be 2x (6.00). Cache read is ~0.1x input.
PRICES = {
    # Default model — verified against the Claude API pricing reference.
    "claude-sonnet-4-6": {
        "input_tokens": 3.00,
        "output_tokens": 15.00,
        "cache_creation_input_tokens": 3.75,   # 5-min TTL write (1.25 x input)
        "cache_read_input_tokens": 0.30,        # cache read (~0.1 x input)
    },
    # Available option (not the default). Verified 2026-07-06: INTRODUCTORY rates,
    # in effect through 2026-08-31. On 2026-09-01 the sticker price takes over
    # ($3.00 / $15.00, identical to 4.6 above) — update this entry then. report.py
    # reprices historical rows from this table, so a rate edit here reprices any
    # past rows recorded under this model too.
    "claude-sonnet-5": {
        "input_tokens": 2.00,
        "output_tokens": 10.00,
        "cache_creation_input_tokens": 2.50,   # 5-min TTL write (1.25 x input)
        "cache_read_input_tokens": 0.20,        # cache read (~0.1 x input)
    },
}

PER_MILLION = 1_000_000


def cost_usd(usage: dict, model: str) -> float:
    """USD cost for one run's token usage.

    `usage` is the agent's running totals dict (any subset of the priced fields;
    missing fields count as zero). Raises KeyError for an unpriced model so a
    silent $0.00 can never slip into the metrics.
    """
    rates = PRICES[model]
    total = sum(usage.get(field, 0) * rate for field, rate in rates.items())
    return total / PER_MILLION
