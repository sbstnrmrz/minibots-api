"""LLM cost estimation.

Add new provider/model pairs as needed. Keys are (PROVIDER_UPPER, model_name).
Rates are USD per 1 000 tokens: (input_rate, output_rate).
Returns None for unknown models so callers can distinguish "no cost data"
from "zero cost".
"""

# (provider_upper, model) → (input_per_1k_usd, output_per_1k_usd)
_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("DEEPSEEK", "deepseek-v4-flash"): (0.00014, 0.00028),
    ("DEEPSEEK", "deepseek-v4-pro"):   (0.00027, 0.00110),
    ("DEEPSEEK", "deepseek-chat"):     (0.00014, 0.00028),
    ("GEMINI",   "gemini-2.0-flash"):  (0.00010, 0.00040),
    ("GEMINI",   "gemini-2.5-flash"):  (0.00015, 0.00060),
    ("GEMINI",   "gemini-2.5-pro"):    (0.00125, 0.01000),
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
