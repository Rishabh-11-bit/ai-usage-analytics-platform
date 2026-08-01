/* TokenTracker — usage tracker + budget bar.
 *
 * Separate from the prompt coach: this measures consumption rather than
 * quality. It counts INPUT tokens when a message is sent and OUTPUT tokens
 * from the rendered response, tags each with the selected model, keeps a
 * month-to-date tally in chrome.storage, draws a usage bar, and (if the
 * tenant is configured) reports COUNTERS ONLY — never prompt/response
 * content — to the TokenTracker dashboard.
 *
 * Honest limits, by design:
 *  - token counts are estimates (~±15%, char/word heuristic, no real BPE)
 *  - context re-send, hidden reasoning tokens and tool calls aren't visible
 *  - DOM selectors below are best-effort per site and may need tuning when
 *    a provider redesigns; everything degrades to "input-only" safely.
 */
(function () {
  "use strict";
  const Coach = (typeof self !== "undefined" && self.TTCoach) || null;
  const estTokens = Coach ? Coach.estimateTokens : t =>
    Math.max(Math.floor((t || "").length / 4), 1);

  const HOST = location.host;
  const monthKey = () => new Date().toISOString().slice(0, 7); // YYYY-MM

  // model price weights (relative burn vs a $1 baseline) — used to turn raw
  // tokens into a comparable "cost units" figure without needing exact $.
  const MODEL_WEIGHTS = [
    [/opus/i, 15], [/gpt-4o|gpt-4\.1|gpt-5/i, 5], [/o3|o1/i, 6],
    [/sonnet/i, 3], [/gemini.*(pro|ultra)/i, 4],
    [/haiku|mini|flash|nano/i, 1],
  ];
  const modelWeight = m => (MODEL_WEIGHTS.find(([re]) => re.test(m || "")) || [, 3])[1];

  const cfgDefaults = {
    enabled: true, showBar: true,
    monthlyBudgetTokens: 1000000,     // configurable per employee
    tenantUrl: "", employeeToken: "",
  };
  let cfg = { ...cfgDefaults };
  const store = {
    async get(d) { try { return await chrome.storage.local.get(d); }
      catch { const o = { ...d }; for (const k in d) { const v = localStorage.getItem("tt_" + k); if (v !== null) o[k] = JSON.parse(v); } return o; } },
    async set(o) { try { await chrome.storage.local.set(o); }
      catch { for (const k in o) localStorage.setItem("tt_" + k, JSON.stringify(o[k])); } },
  };

  // month-to-date usage: { "YYYY-MM": { input, output, messages, byModel:{}, bySite:{} } }
  let usage = {};

  async function loadState() {
    const s = await store.get({ ...cfgDefaults, ttUsage: {} });
    cfg = { enabled: s.enabled, showBar: s.showBar,
      monthlyBudgetTokens: +s.monthlyBudgetTokens || cfgDefaults.monthlyBudgetTokens,
      tenantUrl: s.tenantUrl || "", employeeToken: s.employeeToken || "" };
    usage = s.ttUsage || {};
  }

  function month() {
    const k = monthKey();
    if (!usage[k]) usage[k] = { input: 0, output: 0, messages: 0, byModel: {}, bySite: {} };
    return usage[k];
  }

  function record(inputTok, outputTok, model) {
    const m = month();
    m.input += inputTok; m.output += outputTok; m.messages += 1;
    m.byModel[model] = (m.byModel[model] || 0) + inputTok + outputTok;
    m.bySite[HOST] = (m.bySite[HOST] || 0) + inputTok + outputTok;
    store.set({ ttUsage: usage });
    renderBar();
    queueReport(inputTok, outputTok, model);
  }

  /* ---- model detection (best-effort, per page) ---- */
  const MODEL_NAMES = /\b(gpt-5|gpt-4\.1|gpt-4o|gpt-4|o3|o1|claude[- ]?(?:opus|sonnet|haiku)[\w.\- ]*|opus|sonnet|haiku|gemini[\w.\- ]*(?:pro|flash|ultra)?|mistral|llama)\b/i;
  function detectModel() {
    // ChatGPT & Claude both render the model name in a header switcher button
    const candidates = document.querySelectorAll(
      '[data-testid*="model" i], [aria-label*="model" i], button, [class*="model" i]');
    for (const el of candidates) {
      // check both aria-label and visible text — either may carry the model
      for (const t of [el.getAttribute("aria-label"), el.textContent]) {
        const s = (t || "").trim();
        if (s && s.length < 60) { const m = s.match(MODEL_NAMES); if (m) return m[0].toLowerCase(); }
      }
    }
    if (/claude\.ai/.test(HOST)) return "claude-sonnet";
    if (/chatgpt|openai/.test(HOST)) return "gpt-4o";
    if (/gemini/.test(HOST)) {
      // Gemini's chip shows just the tier ("2.5 Pro", "1.5 Flash") — read it
      const chip = document.querySelector('[aria-label*="model" i], [class*="model" i]');
      const txt = (chip ? chip.textContent : "").toLowerCase();
      if (/flash/.test(txt)) return "gemini-flash";
      if (/ultra/.test(txt)) return "gemini-ultra";
      return "gemini-pro";
    }
    return "unknown";
  }

  /* ---- input capture on send (selector-agnostic) ----
   * We don't rely on any site's send-button markup. We track the composer
   * text as it's typed, and treat a non-empty → empty transition as a send.
   * Enter (without shift) on the composer is an extra immediate trigger.
   * This works on ChatGPT, Claude, Gemini, Copilot alike. */
  const isPromptBox = el => el && el.closest &&
    el.closest('textarea, [contenteditable="true"]');
  const readBox = el => !el ? "" : el.tagName === "TEXTAREA" ? el.value : (el.innerText || "");

  let pendingInput = 0, pendingInputChars = 0, pendingModel = "unknown";
  let awaitingResponse = false, lastComposerText = "", clearArmed = false;

  function onSend(text) {
    const t = (text || "").trim();
    if (!t || awaitingResponse) return;
    pendingInput = estTokens(t);
    pendingInputChars = t.length;
    pendingModel = detectModel();
    awaitingResponse = true;
    lastComposerText = "";
    clearArmed = false;
    watchForResponse();
  }

  // track composer text; fire on the clear that follows a send
  document.addEventListener("input", e => {
    if (!cfg.enabled) return;
    const box = isPromptBox(e.composedPath ? e.composedPath()[0] : e.target);
    if (!box) return;
    const t = readBox(box).trim();
    if (t) { lastComposerText = t; clearArmed = true; }
    else if (clearArmed && lastComposerText) { onSend(lastComposerText); }
  }, true);

  // Enter (no shift) submits on all these sites — immediate, more reliable
  document.addEventListener("keydown", e => {
    if (!cfg.enabled || e.key !== "Enter" || e.shiftKey) return;
    const box = isPromptBox(e.target) || isPromptBox(document.activeElement);
    if (box) { const t = readBox(box).trim(); if (t) { lastComposerText = t;
      // defer so the value is captured before the field clears
      setTimeout(() => onSend(t), 0); } }
  }, true);

  /* ---- output capture: measure how much the main content grows ----
   * Selector-agnostic: snapshot the main region's text length at send,
   * then watch it grow and settle. The delta minus the echoed prompt is
   * the response. Robust to any provider's message markup. */
  /* Measure the page's total visible text length, read FRESH each time —
   * never hold a node reference. SPA frameworks (Gemini/Angular, ChatGPT/
   * React) swap the content node out while streaming, so a cached element
   * goes stale and reads 0 growth. document.body always persists and its
   * innerText includes the streamed response. We subtract a static baseline
   * so page chrome doesn't count. */
  const pageTextLen = () => (document.body.innerText || "").length;

  function watchForResponse() {
    const baseline = pageTextLen();
    // The site echoes the user's prompt into the DOM instantly; that early
    // growth is NOT the response. We only consider the exchange "done" once
    // growth clearly exceeds the echoed prompt AND then goes quiet — so the
    // model's thinking pause before it starts streaming can't finish us early.
    const echoThreshold = pendingInputChars * 1.5 + 40;
    let peak = baseline, lastLen = baseline, elapsed = 0, lastGrowthAt = 0;
    const tick = () => {
      if (!awaitingResponse) return;
      elapsed += 400;
      const len = pageTextLen();
      if (len > peak) peak = len;
      if (len > lastLen + 4) { lastLen = len; lastGrowthAt = elapsed; }
      const grownNow = Math.max(peak - baseline, 0);
      const hasResponse = grownNow > echoThreshold;   // real content, not just echo
      const quietFor = elapsed - lastGrowthAt;
      const done = hasResponse && quietFor >= 2500 && elapsed >= 4000;
      const noResponse = !hasResponse && elapsed >= 20000; // only echo → UI card
      if (done || noResponse || elapsed >= 180000) return finish();
      setTimeout(tick, 400);
    };
    const finish = () => {
      if (!awaitingResponse) return;
      awaitingResponse = false;
      const grown = Math.max(Math.max(pageTextLen(), peak) - baseline, 0);
      const outChars = Math.max(grown - pendingInputChars, 0);
      const outTok = Math.floor(outChars / 4);
      record(pendingInput, outTok, pendingModel);
      console.log("[TokenTracker Usage] recorded:", pendingInput, "in /", outTok,
        "out · model", pendingModel, "· baseline", baseline, "→ peak", peak,
        "(grew", grown, "chars)");
    };
    setTimeout(tick, 400);
  }

  /* ---- telemetry: counters only, batched ---- */
  let reportQueue = [], reportTimer = null;
  function queueReport(inputTok, outputTok, model) {
    if (!cfg.tenantUrl || !cfg.employeeToken) return; // personal mode: local only
    reportQueue.push({ ts: new Date().toISOString(), site: HOST, model,
      input_tokens: inputTok, output_tokens: outputTok });
    clearTimeout(reportTimer);
    reportTimer = setTimeout(flushReports, 4000);
  }
  async function flushReports() {
    if (!reportQueue.length) return;
    const batch = reportQueue.splice(0);
    try {
      await fetch(cfg.tenantUrl.replace(/\/$/, "") + "/api/telemetry", {
        method: "POST",
        headers: { "Content-Type": "application/json",
          "Authorization": "Bearer " + cfg.employeeToken },
        body: JSON.stringify({ events: batch }),
      });
    } catch { reportQueue.unshift(...batch); } // retry next time
  }

  /* ---- the usage bar ---- */
  let barHost, barRoot;
  function renderBar() {
    if (!cfg.showBar) { if (barHost) barHost.style.display = "none"; return; }
    if (!barHost) {
      barHost = document.createElement("div");
      barHost.style.cssText = "position:fixed; z-index:2147483646; bottom:14px; right:14px;";
      barRoot = barHost.attachShadow({ mode: "open" });
      document.body.appendChild(barHost);
    }
    barHost.style.display = "block";
    const m = month();
    const used = m.input + m.output;
    const budget = Math.max(cfg.monthlyBudgetTokens, 1);
    const pct = Math.min(used / budget * 100, 100);
    const remaining = Math.max(budget - used, 0);
    const state = pct >= 100 ? "over" : pct >= 80 ? "warn" : "ok";
    const fmt = n => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : n;
    const color = { ok: "#3ecf6e", warn: "#fab219", over: "#e66767" }[state];
    barRoot.innerHTML = `
      <style>
        * { box-sizing: border-box; font-family: system-ui, sans-serif; }
        .wrap { width: 220px; background:#1a1a19; color:#e8e8e4; border:1px solid rgba(255,255,255,.18);
                border-radius:10px; padding:10px 12px; box-shadow:0 4px 18px rgba(0,0,0,.4); font-size:12px; }
        .top { display:flex; justify-content:space-between; margin-bottom:6px; }
        .top .lbl { color:#b5b4ac; }
        .top .val { font-weight:600; font-variant-numeric:tabular-nums; }
        .track { height:8px; background:#2c2c2a; border-radius:5px; overflow:hidden; }
        .fill { height:100%; width:${pct}%; background:${color}; border-radius:5px; transition:width .3s ease; }
        .foot { margin-top:6px; color:#8a8983; font-size:10.5px; display:flex; justify-content:space-between; }
        .est { color:#8a8983; }
      </style>
      <div class="wrap" title="Estimated month-to-date AI usage — TokenTracker">
        <div class="top"><span class="lbl">AI usage · ${monthKey()}</span>
          <span class="val">${Math.round(pct)}%</span></div>
        <div class="track"><div class="fill"></div></div>
        <div class="foot"><span>${fmt(used)} / ${fmt(budget)} tok</span>
          <span>${fmt(remaining)} left</span></div>
        <div class="foot"><span class="est">${m.messages} msgs · est.</span>
          <span class="est">${cfg.tenantUrl ? "reporting" : "local only"}</span></div>
      </div>`;
  }

  /* live config updates + boot */
  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      loadState().then(renderBar);
    });
  } catch {}

  loadState().then(() => {
    console.log("[TokenTracker Usage] v0.4.5 tracking on", HOST);
    renderBar();
  });
})();
