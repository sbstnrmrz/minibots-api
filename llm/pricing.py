"""LLM cost estimation.

Add new provider/model pairs as needed. Keys are (PROVIDER_UPPER, model_name).
Rates are USD per 1 000 tokens: (input_rate, output_rate).
Returns None for unknown models so callers can distinguish "no cost data"
from "zero cost".
"""

# (provider_upper, model) → (input_per_1k_usd, output_per_1k_usd)
# Sources (verified 2026-05-24):
#   DeepSeek: https://api-docs.deepseek.com/quick_start/pricing
#   Gemini:   https://ai.google.dev/gemini-api/docs/pricing
_RATES: dict[tuple[str, str], tuple[float, float]] = {
    # DeepSeek — prices are cache-miss (full) input rate
    ("DEEPSEEK", "deepseek-v4-flash"): (0.00014,   0.00028),   # $0.14 / $0.28 per 1M
    ("DEEPSEEK", "deepseek-v4-pro"):   (0.000435,  0.00087),   # $0.435 / $0.87 per 1M (75% promo)
    ("DEEPSEEK", "deepseek-chat"):     (0.00014,   0.00028),   # alias for v4-flash tier

    # Gemini — standard tier, prompts ≤200k tokens
    ("GEMINI",   "gemini-2.0-flash"):      (0.00010,  0.00040),  # deprecated Jun 2026; $0.10 / $0.40 per 1M
    ("GEMINI",   "gemini-2.5-flash"):      (0.00030,  0.00250),  # $0.30 / $2.50 per 1M
    ("GEMINI",   "gemini-2.5-flash-lite"): (0.00010,  0.00040),  # $0.10 / $0.40 per 1M
    ("GEMINI",   "gemini-2.5-pro"):        (0.00125,  0.01000),  # $1.25 / $10.00 per 1M
}


def compute_cost(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Return estimated USD cost, or None if the model is not in the table."""
    key = (provider.upper(), model)
    if key not in _RATES:
        return None
    inp_rate, out_rate = _RATES[key]
    cost = inp_rate * prompt_tokens / 1000 + out_rate * completion_tokens / 1000
    return round(cost, 8)
