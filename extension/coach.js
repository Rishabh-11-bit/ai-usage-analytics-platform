/* TokenTracker prompt coach — JS port of coach.py (same heuristics, same
 * numbers) plus a deterministic quick-fix rewriter. Runs entirely in the
 * browser: no prompt content ever leaves the page. */
(function (root) {
  "use strict";

  const FILLER_PHRASES = [
    "please", "kindly", "could you", "would you", "can you",
    "i would like you to", "i want you to", "go ahead and",
    "make sure to", "be sure to", "feel free to", "thank you",
  ];
  const WORDY_SWAPS = [
    ["in order to", "to"], ["due to the fact that", "because"],
    ["at this point in time", "now"], ["in the event that", "if"],
    ["basically", ""], ["actually", ""], ["essentially", ""],
    ["simply", ""], ["very", ""], ["really", ""],
  ];
  const wordyRe = w => new RegExp("\\b" + escapeRe(w) + "\\b ?", "gi");
  const CONSTRAINT_RE = /\b(max|limit|concise|brief|bullet|json|format|schema|words?|sentences?|paragraphs?|short|table|list only)\b/i;
  const STRUCTURE_RE = /(^#{1,4} |^[-*] |^\d+\. |<\w+>|```|^---)/m;
  const BASELINE_OUTPUT_TOKENS = 800;

  function estimateTokens(text) {
    const words = text.split(/\s+/).filter(Boolean).length;
    return Math.max(Math.floor(text.length / 4), Math.floor(words * 1.3), 1);
  }

  const count = (haystack, needle) => haystack.split(needle).length - 1;

  function analyze(prompt) {
    const text = prompt.trim();
    const originalTokens = estimateTokens(text);
    const low = text.toLowerCase();
    const issues = [];

    // 1. Politeness / filler phrases
    const fillerHits = FILLER_PHRASES.reduce((a, p) => a + count(low, p), 0);
    if (fillerHits >= 3) {
      issues.push({
        type: "filler_phrases", kind: "input", fixable: true,
        tokensSaved: Math.floor(fillerHits * 2.5),
        detail: `${fillerHits} filler/politeness phrases ('please', 'make sure to'…). ` +
          "Use direct imperatives: 'Summarize X', not 'Could you please summarize X'.",
      });
    }

    // 2. Wordy constructions
    const wordyHits = WORDY_SWAPS.reduce((a, [w]) => a + (low.match(wordyRe(w)) || []).length, 0);
    if (wordyHits >= 3) {
      issues.push({
        type: "wordy_constructions", kind: "input", fixable: true,
        tokensSaved: wordyHits * 2,
        detail: `${wordyHits} wordy constructions ('in order to' → 'to', ` +
          "'due to the fact that' → 'because', filler adverbs).",
      });
    }

    // 3. Duplicate lines
    const lines = text.split("\n").map(l => l.replace(/\s+/g, " ").trim().toLowerCase())
      .filter(l => l.length > 30);
    const seen = new Set();
    let dupTokens = 0;
    for (const l of lines) {
      if (seen.has(l)) dupTokens += estimateTokens(l);
      seen.add(l);
    }
    if (dupTokens > 10) {
      issues.push({
        type: "duplicate_content", kind: "input", fixable: true, tokensSaved: dupTokens,
        detail: `~${dupTokens} tokens of repeated lines. State each instruction once.`,
      });
    }

    // 4. Verbose persona preamble
    const persona = low.match(/^(you are (a|an|the) [^.!\n]{80,})/);
    if (persona) {
      const excess = estimateTokens(persona[1]) - 15;
      if (excess > 5) {
        issues.push({
          type: "verbose_persona", kind: "input", fixable: false, tokensSaved: excess,
          detail: "Long persona preamble. One short line ('You are a senior Python " +
            "reviewer.') performs the same — trim the adjectives.",
        });
      }
    }

    // 5. Excessive emphasis
    const caps = (text.match(/\b[A-Z]{4,}\b/g) || []).length;
    const bangs = count(text, "!!") +
      (low.match(/\b(very important|critical|must absolutely)\b/g) || []).length;
    if (caps + bangs >= 4) {
      issues.push({
        type: "excessive_emphasis", kind: "input", fixable: false,
        tokensSaved: (caps + bangs) * 2,
        detail: `${caps + bangs} emphasis markers (ALL-CAPS, 'VERY IMPORTANT', '!!'). ` +
          "Keep one 'Requirements:' block instead.",
      });
    }

    // 6. Unstructured context dump
    const bigParas = text.split(/\n\s*\n/).filter(p => p.split(/\s+/).length > 120);
    if (bigParas.length && !STRUCTURE_RE.test(text)) {
      const blob = bigParas.reduce((a, p) => a + estimateTokens(p), 0);
      issues.push({
        type: "unstructured_context", kind: "input", fixable: false,
        tokensSaved: Math.floor(blob * 0.12),
        detail: `~${blob} tokens of unstructured prose. Delimited sections (headers, ` +
          "bullets) cut connective filler ~10-15% and improve instruction-following.",
      });
    }

    // 7. Missing output constraint
    let outputSaved = 0;
    if (originalTokens > 60 && !CONSTRAINT_RE.test(text)) {
      outputSaved = Math.floor(BASELINE_OUTPUT_TOKENS * 0.35);
      issues.push({
        type: "no_output_constraint", kind: "output", fixable: true,
        tokensSaved: outputSaved,
        detail: "No output constraint (length, format). Output tokens cost 4-5x " +
          "input — add 'Answer in <200 words' or a schema.",
      });
    }

    const inputSaved = Math.min(
      issues.filter(i => i.kind === "input").reduce((a, i) => a + i.tokensSaved, 0),
      Math.floor(originalTokens * 0.6));

    return {
      originalTokens,
      optimizedTokens: originalTokens - inputSaved,
      inputTokensSaved: inputSaved,
      outputTokensSaved: outputSaved,
      reductionPct: Math.round(inputSaved / originalTokens * 1000) / 10,
      issues,
    };
  }

  /* ---- protection for technical content ----
   * Code fences, inline code, and quoted spans are frozen before any
   * rewrite and restored after — the rewriter never edits code, data,
   * or exact phrases the user put in quotes. */
  const PROTECT_RE = /```[\s\S]*?```|`[^`\n]+`|"[^"\n]{2,}"|'[^'\n]{6,}'/g;

  function protect(text) {
    const spans = [];
    const masked = text.replace(PROTECT_RE, m => {
      spans.push(m);
      return `\u0000${spans.length - 1}\u0000`;
    });
    return { masked, spans };
  }

  const restore = (text, spans) =>
    text.replace(/\u0000(\d+)\u0000/g, (_, i) => spans[+i]);

  /* Deterministic, safe rewrites only: fillers, wordy swaps, duplicate
   * lines, whitespace. Emphasis/persona/structure stay manual — auto-
   * editing those risks changing meaning. */
  function quickFix(prompt) {
    const shield = protect(prompt);
    let text = shield.masked;

    for (const [wordy, slim] of WORDY_SWAPS) {
      text = text.replace(wordyRe(wordy), slim ? slim + " " : "");
    }
    text = text
      .replace(/\b(could|would|can) you (please )?/gi, "")
      .replace(/\bi (would like|want) you to /gi, "")
      .replace(/\b(please|kindly)[ ,]/gi, "")
      .replace(/\b(go ahead and|make sure to|be sure to|feel free to) /gi, "")
      .replace(/!{2,}/g, ".");
    // gratitude: drop sentences that ARE thanks; trim clauses that END in
    // thanks (never delete a sentence that also carries an instruction)
    text = dropSentences(text, [/^\W*(also\s+)?(thanks?\b|thank you\b)/i,
                                /^\W*i (really |truly )?appreciate\b/i]);
    text = text.replace(/,\s*(thanks?\b|thank you\b|i (really |truly )?appreciate (it|this)\b)[^,.!?\n]*/gi, "");

    const seen = new Set();
    text = text.split("\n").filter(line => {
      const norm = line.replace(/\s+/g, " ").trim().toLowerCase();
      if (norm.length > 30 && seen.has(norm)) return false;
      seen.add(norm);
      return true;
    }).join("\n");

    text = text.replace(/[ \t]{2,}/g, " ").replace(/\n{3,}/g, "\n\n").trim();
    // sentence-start capitalization can break after removals
    text = text.replace(/(^|[.!?]\s+)([a-z])/g, (m, pre, ch) => pre + ch.toUpperCase());
    text = restore(text, shield.spans);

    const before = estimateTokens(prompt);
    const after = estimateTokens(text);
    return { text, tokensSaved: Math.max(before - after, 0), before, after };
  }

  function addConstraint(prompt) {
    return prompt.trim() + "\n\nAnswer concisely (under 200 words).";
  }

  /* ---- deep clean: the aggressive tier ---- */

  const splitSentences = text =>
    text.split(/(?<=[.!?])\s+/).filter(s => s.trim().length);

  /* Sentence ops run per line so newlines, bullets and headers survive —
   * destroying structure would make the analyzer flag our own output. */
  function dropSentences(text, patterns) {
    return text.split("\n").map(line =>
      splitSentences(line)
        .filter(s => !patterns.some(p => p.test(s)))
        .join(" ")
    ).join("\n").replace(/\n{3,}/g, "\n\n").trim();
  }

  const GREETING_RE = [
    /^\s*(hello|hi|hey|greetings)\b/i,
    /\bhope you('re| are)? (doing )?(well|great|good)\b/i,
  ];
  const ENCOURAGEMENT_RE = [
    /\btake your time\b/i, /\bdig as deep as\b/i, /\bdo your best\b/i,
    /\byou can do (this|it)\b/i, /\bno rush\b/i,
  ];
  const PUFFERY_RE = /\b(highly intelligent|world-?class|extremely experienced|incredibly (talented|skilled)|brilliant|seasoned|expert|amazing|exceptional|extraordinary|masterful|elite|renowned|award-winning|veteran|legendary|genius|best-in-class)\b/gi;
  const ACRONYMS = new Set(["JSON", "API", "CSV", "SQL", "HTML", "CSS", "URL",
    "HTTP", "HTTPS", "AI", "ML", "ID", "USD", "EUR", "INR", "KPI", "ROI",
    "CEO", "CTO", "CFO", "PDF", "XML", "YAML", "REST", "CRUD", "GDPR", "SLA",
    "JWT", "TLS", "SSL", "SSH", "ACID", "GRPC", "RPC", "TCP", "UDP", "DNS",
    "AWS", "GCP", "CPU", "GPU", "RAM", "SDK", "CLI", "ETL", "ORM", "CDN",
    "IAM", "VPC", "SSO", "SCIM", "SAML", "CORS", "CSP", "XSS", "CSRF",
    "MQTT", "AMQP", "OLTP", "OLAP", "UUID", "REGEX", "CIDR", "NAT", "BGP",
    "OSPF", "VLAN", "SNMP", "LDAP", "SMTP", "IMAP", "FTP", "SFTP", "K8S"]);

  function deepClean(prompt) {
    const shield = protect(quickFix(prompt).text);
    let text = shield.masked;

    // greetings, sign-offs, cheerleading — whole sentences, zero signal
    text = dropSentences(text, [...GREETING_RE, ...ENCOURAGEMENT_RE]);

    // compress the persona: strip puffery adjectives and résumé clauses
    text = text.replace(/(you are (?:a|an|the)\b[^.!?\n]*)/gi, m => m
      .replace(PUFFERY_RE, "")
      .replace(/\bwith (decades|years|a lifetime) of experience\b[^.!?\n]*/gi, "")
      .replace(/\bin every [^.!?\n]*\b(imaginable|known to (man|humanity))[^.!?\n]*/gi, ""));

    // "it is important that you look at X" → "look at X"
    text = text.replace(/\bit('s| is) (absolutely |very |really |critically )?(important|critical|essential|crucial|vital) that (you )?/gi, "");
    text = text.replace(/\b(absolutely|extremely|incredibly|totally) /gi, "");

    // stop shouting (acronyms stay)
    text = text.replace(/\b[A-Z]{3,}\b/g, w => ACRONYMS.has(w) ? w : w.toLowerCase());

    // drop a sentence that is fully contained in another (keep the longer),
    // comparing across the whole prompt but rebuilding line by line
    const norm = s => s.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
    const all = text.split("\n").flatMap(splitSentences);
    const isDup = (s, i) => {
      const n = norm(s);
      if (n.length <= 6) return false;
      return all.some((o, j) => j !== i && norm(o).includes(n) &&
        (norm(o).length > n.length || j < i));
    };
    let idx = 0;
    text = text.split("\n").map(line =>
      splitSentences(line).filter(() => !isDup(all[idx], idx++)).join(" ")
    ).join("\n").replace(/\n{3,}/g, "\n\n");

    // tidy punctuation/spacing artifacts left by removals (newlines are
    // structure — never collapse them here)
    text = text
      .replace(/ *, *(?=,)/g, "")
      .replace(/\b(a|an|the) *, */gi, "$1 ")
      .replace(/[ \t]+([.,!?])/g, "$1")
      .replace(/,{2,}/g, ",")
      .replace(/[ \t]{2,}/g, " ")
      .replace(/([.!?]) *\1+/g, "$1")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    text = text.replace(/(^|[.!?]\s+)([a-z])/g, (m, pre, ch) => pre + ch.toUpperCase());
    text = restore(text, shield.spans);

    // the one thing worth ADDING: an output constraint, if none exists
    if (!CONSTRAINT_RE.test(text)) text = addConstraint(text);

    const before = estimateTokens(prompt);
    const after = estimateTokens(text);
    return { text, tokensSaved: Math.max(before - after, 0), before, after };
  }

  const escapeRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  const TTCoach = { estimateTokens, analyze, quickFix, deepClean, addConstraint };
  if (typeof module !== "undefined" && module.exports) module.exports = TTCoach;
  else root.TTCoach = TTCoach;
})(typeof self !== "undefined" ? self : this);
