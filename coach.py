"""Prompt coach: heuristic token-reduction analysis for a pasted prompt.

No LLM call needed — detects common bloat patterns (fillers, duplicate
lines, verbose personas, missing output constraints) and estimates the
token + dollar savings of fixing them. All numbers are estimates.
"""
import re

from pricing import PRICING

# Assumed output size when the prompt itself gives no signal (tokens).
BASELINE_OUTPUT_TOKENS = 800

FILLER_PHRASES = [
    "please", "kindly", "could you", "would you", "can you",
    "i would like you to", "i want you to", "go ahead and",
    "make sure to", "be sure to", "feel free to", "thank you",
]
WORDY_SWAPS = [
    ("in order to", "to"), ("due to the fact that", "because"),
    ("at this point in time", "now"), ("in the event that", "if"),
    ("basically", ""), ("actually", ""), ("essentially", ""),
    ("simply", ""), ("very", ""), ("really", ""),
]
CONSTRAINT_SIGNALS = re.compile(
    r"\b(max|limit|concise|brief|bullet|json|format|schema|words?|sentences?|"
    r"paragraphs?|short|table|list only)\b", re.I)
STRUCTURE_SIGNALS = re.compile(r"(^#{1,4} |^[-*] |^\d+\. |<\w+>|```|^---)", re.M)


def estimate_tokens(text):
    """Rough tokenizer-free estimate: ~4 chars or ~1.3 tokens per word."""
    words = len(text.split())
    return max(len(text) // 4, int(words * 1.3), 1)


def _phrase_tokens(phrase):
    return max(1, len(phrase.split()))


def analyze(prompt, provider="anthropic", model="claude-sonnet-5", runs_per_day=50):
    text = prompt.strip()
    original_tokens = estimate_tokens(text)
    low = text.lower()
    issues = []

    # 1. Politeness / filler phrases
    filler_hits = sum(low.count(p) for p in FILLER_PHRASES)
    if filler_hits >= 3:
        saved = int(filler_hits * 2.5)
        issues.append({
            "type": "filler_phrases", "kind": "input", "tokens_saved": saved,
            "detail": (f"{filler_hits} filler/politeness phrases ('please', 'make sure to', "
                       "'I would like you to'…). Models don't need politeness — use direct "
                       "imperatives: 'Summarize X', not 'Could you please summarize X'."),
        })

    # 2. Wordy constructions (word-boundary so 'every' doesn't count as 'very')
    wordy_hits = sum(len(re.findall(rf"\b{re.escape(a)}\b", low)) for a, _ in WORDY_SWAPS)
    if wordy_hits >= 3:
        issues.append({
            "type": "wordy_constructions", "kind": "input", "tokens_saved": wordy_hits * 2,
            "detail": (f"{wordy_hits} wordy constructions ('in order to' → 'to', "
                       "'due to the fact that' → 'because', filler adverbs). "
                       "Each costs tokens on every run."),
        })

    # 3. Duplicate / near-duplicate lines
    lines = [re.sub(r"\s+", " ", l).strip().lower() for l in text.splitlines()]
    lines = [l for l in lines if len(l) > 30]
    seen, dup_tokens = set(), 0
    for l in lines:
        if l in seen:
            dup_tokens += estimate_tokens(l)
        seen.add(l)
    if dup_tokens > 10:
        issues.append({
            "type": "duplicate_content", "kind": "input", "tokens_saved": dup_tokens,
            "detail": (f"~{dup_tokens} tokens of repeated lines. Repeating an instruction "
                       "doesn't make the model follow it better — state it once, clearly."),
        })

    # 4. Verbose persona preamble
    m = re.match(r"(you are (a|an|the) [^.!\n]{80,})", low)
    if m:
        excess = estimate_tokens(m.group(1)) - 15
        if excess > 5:
            issues.append({
                "type": "verbose_persona", "kind": "input", "tokens_saved": excess,
                "detail": ("Long persona preamble ('You are a world-class expert…'). "
                           "One short line ('You are a senior Python reviewer.') performs "
                           "the same — trim the adjectives."),
            })

    # 5. Excessive emphasis
    caps = len(re.findall(r"\b[A-Z]{4,}\b", text))
    bangs = text.count("!!") + len(re.findall(r"\b(very important|critical|must absolutely)\b", low))
    if caps + bangs >= 4:
        issues.append({
            "type": "excessive_emphasis", "kind": "input", "tokens_saved": (caps + bangs) * 2,
            "detail": (f"{caps + bangs} emphasis markers (ALL-CAPS words, 'VERY IMPORTANT', '!!'). "
                       "Shouting costs tokens and dilutes real constraints — keep one "
                       "'Requirements:' block instead."),
        })

    # 6. Unstructured context dump
    paras = [p for p in re.split(r"\n\s*\n", text) if len(p.split()) > 120]
    if paras and not STRUCTURE_SIGNALS.search(text):
        blob = sum(estimate_tokens(p) for p in paras)
        issues.append({
            "type": "unstructured_context", "kind": "input", "tokens_saved": int(blob * 0.12),
            "detail": (f"~{blob} tokens of unstructured prose. Delimited sections "
                       "(headers, bullets, XML tags) let you cut connective filler "
                       "~10-15% and improve instruction-following."),
        })

    # 7. Missing output constraint → output-token savings (priced higher)
    output_saved = 0
    if original_tokens > 60 and not CONSTRAINT_SIGNALS.search(text):
        output_saved = int(BASELINE_OUTPUT_TOKENS * 0.35)
        issues.append({
            "type": "no_output_constraint", "kind": "output", "tokens_saved": output_saved,
            "detail": ("No output constraint found (length, format, or structure). "
                       "Unconstrained answers run long — and output tokens cost 4-5x "
                       "input. Add 'Answer in <200 words' or a JSON schema."),
        })

    input_saved = min(sum(i["tokens_saved"] for i in issues if i["kind"] == "input"),
                      int(original_tokens * 0.6))
    optimized_tokens = original_tokens - input_saved

    # Cost projection
    inp_price, _, out_price = PRICING.get((provider, model), (3.0, 1.5, 15.0))
    per_run_saved = (input_saved * inp_price + output_saved * out_price) / 1_000_000
    monthly_savings = per_run_saved * runs_per_day * 30

    return {
        "original_tokens": original_tokens,
        "optimized_tokens": optimized_tokens,
        "input_tokens_saved": input_saved,
        "output_tokens_saved": output_saved,
        "reduction_pct": round(input_saved / original_tokens * 100, 1),
        "issues": issues,
        "model": f"{provider}/{model}",
        "runs_per_day": runs_per_day,
        "est_monthly_savings_usd": round(monthly_savings, 2),
    }
