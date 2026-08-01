# TokenTracker Prompt Coach — browser extension

Grammarly-for-prompts: while an employee types into ChatGPT, Claude, Gemini,
Copilot or Poe, a small badge shows the prompt's token count and any fixable
bloat (filler phrases, wordy constructions, duplicate lines, missing output
constraints…). One click applies the safe rewrites; a second button appends
an output constraint. Lifetime tokens-saved stats live in the popup.

**Privacy:** all analysis runs locally (`coach.js`, a port of the backend's
`coach.py`). Prompt content never leaves the browser; the only stored data
is two counters (fixes applied, tokens saved) in `chrome.storage.local`.

## Install (unpacked)

1. Chrome → `chrome://extensions` → enable **Developer mode**
2. **Load unpacked** → select this `extension/` folder
3. Open chatgpt.com or claude.ai and start typing a long prompt —
   the badge appears above the input once it exceeds ~30 tokens and
   has at least one finding.

## Test without installing

Open `test.html` via a local server (`python -m http.server`) — it loads the
same scripts against a textarea and a contenteditable div.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | MV3 manifest (content script on AI chat hosts) |
| `coach.js` | Local analysis engine + quick-fix rewriter |
| `content.js` | Badge/panel UI (shadow DOM), input attachment, stats |
| `popup.html/js` | Toolbar popup: stats + enable toggle |
| `options.html/js` | Options page: stats, toggle, reset |
| `test.html` | Harness to exercise the UI outside Chrome |

## Usage tracking + budget bar (v0.4.0)

Beyond coaching, the extension measures consumption:

- **Counts input on send and output from the rendered reply** — so it captures
  the "depth of task" effect (a short prompt with a long answer), not just what
  was typed. Detects the selected model to weight usage.
- **Month-to-date usage bar** (bottom-right): tokens used vs. a configurable
  monthly budget, colour-coded green/amber/red. Answers "how much have I used /
  how much is left" — like the quota bars people know, but budget-based.
- **Workspace reporting (optional):** if a TokenTracker URL + employee token are
  set in Options, the extension POSTs **counters only** (tokens, model, site,
  timestamp — never content) to `/api/telemetry`. Left blank, everything stays
  local. This is what powers the dashboard's membership-rightsizing panel for
  companies on personal/free accounts with no provider admin API.

Honest limits (shown in the UI as "est."): token counts are heuristic (~±15%),
context re-send / hidden reasoning / tool calls aren't visible, and it's
browser-only. Good enough for utilization and triage; the gateway gives exact
numbers when precision matters.

Test outside Chrome: `test-usage.html` simulates a chat UI (model switcher,
streaming replies, send button) so the tracker and bar can be exercised.

## Roadmap

- Report anonymized counters (not content) to a TokenTracker tenant so the
  dashboard can show org-wide "tokens saved at the keyboard"
- Per-site enable/disable; custom filler dictionaries per org
- Firefox port (MV3 compatible)
