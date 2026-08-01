"""Per-million-token pricing (USD). Update as providers change prices."""

PRICING = {
    # provider, model: (input, cached_input, output) per 1M tokens
    ("openai", "gpt-4o"): (2.50, 1.25, 10.00),
    ("openai", "gpt-4o-mini"): (0.15, 0.075, 0.60),
    ("openai", "o3"): (2.00, 0.50, 8.00),
    ("anthropic", "claude-sonnet-5"): (3.00, 0.30, 15.00),
    ("anthropic", "claude-haiku-4-5"): (1.00, 0.10, 5.00),
    ("anthropic", "claude-opus-4-8"): (15.00, 1.50, 75.00),
}

# Cheaper substitute suggestions for the optimization engine
CHEAPER_ALTERNATIVE = {
    "claude-opus-4-8": "claude-sonnet-5",
    "claude-sonnet-5": "claude-haiku-4-5",
    "gpt-4o": "gpt-4o-mini",
    "o3": "gpt-4o-mini",
}


def cost_usd(provider, model, input_tokens, cached_tokens, output_tokens):
    inp, cached, out = PRICING.get((provider, model), (3.0, 1.5, 15.0))
    return (
        (input_tokens - cached_tokens) * inp
        + cached_tokens * cached
        + output_tokens * out
    ) / 1_000_000
