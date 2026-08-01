/* TokenTracker Prompt Coach — content script.
 * Attaches a small badge to the active prompt input (textarea or
 * contenteditable). Analysis runs locally via coach.js; the badge shows
 * live token count + issue count, the panel details findings, and
 * "Quick fix" / "Deep clean" rewrite in place.
 *
 * Resilience notes: SPA editors (ChatGPT's ProseMirror, Claude's) replace
 * their input element on re-render, so we never trust a stored element —
 * every input event re-resolves the editable root, and analysis re-checks
 * that the target is still connected. */
(function () {
  "use strict";
  const Coach = (typeof self !== "undefined" && self.TTCoach) || null;
  if (!Coach) { console.warn("[TokenTracker Coach] coach.js did not load"); return; }
  console.log("[TokenTracker Coach] v0.4.0 active on", location.host);

  const MIN_TOKENS = 30;      // findings threshold — don't nag on short prompts
  const SHOW_TOKENS = 10;     // feedback threshold — show "looks lean" above this
  const DEBOUNCE_MS = 300;
  const IDLE_MS = 1400;

  const storage = {
    async get(defaults) {
      try { return await chrome.storage.local.get(defaults); }
      catch { // test harness fallback
        const out = { ...defaults };
        for (const k in defaults) {
          const v = localStorage.getItem("tt_" + k);
          if (v !== null) out[k] = JSON.parse(v);
        }
        return out;
      }
    },
    async set(obj) {
      try { await chrome.storage.local.set(obj); }
      catch { for (const k in obj) localStorage.setItem("tt_" + k, JSON.stringify(obj[k])); }
    },
  };

  let host, root, chip, panel;
  let target = null, debounceTimer = null, idleTimer = null, lastAnalysis = null;
  let autoKey = "", manuallyClosed = false;
  let enabled = true;
  storage.get({ enabled: true }).then(s => { enabled = s.enabled; });

  function ensureUi() {
    if (host) return;
    host = document.createElement("div");
    host.id = "tt-coach-host";
    host.style.cssText = "position:fixed; z-index:2147483647; top:0; left:0;";
    root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        * { box-sizing: border-box; font-family: system-ui, sans-serif; }
        .chip {
          position: fixed; display: none; align-items: center; gap: 6px;
          background: #1a1a19; color: #fff; border: 1px solid rgba(255,255,255,.18);
          border-radius: 999px; padding: 4px 12px; font-size: 12px; cursor: pointer;
          box-shadow: 0 2px 10px rgba(0,0,0,.35); user-select: none; white-space: nowrap;
          transition: opacity .15s ease, background .2s ease;
          opacity: 0;
        }
        .chip.show { opacity: 1; }
        .chip .dot { width: 8px; height: 8px; border-radius: 50%; background: #3ecf6e;
                     transition: background .2s ease; flex: none; }
        .chip.warn .dot { background: #fab219; }
        .chip.bad .dot { background: #e66767; }
        .chip.busy .dot { animation: tt-pulse .9s ease-in-out infinite; }
        @keyframes tt-pulse { 0%,100% { opacity: 1; } 50% { opacity: .25; } }
        .panel {
          position: fixed; display: none; width: 340px; max-height: 380px; overflow: auto;
          background: #1a1a19; color: #e8e8e4; border: 1px solid rgba(255,255,255,.18);
          border-radius: 10px; padding: 12px; font-size: 12px;
          box-shadow: 0 8px 30px rgba(0,0,0,.45);
          opacity: 0; transition: opacity .15s ease;
        }
        .panel.show { opacity: 1; }
        .panel h3 { margin: 0 0 8px; font-size: 12.5px; display: flex; justify-content: space-between; }
        .panel h3 .save { color: #3ecf6e; }
        .issue { border-top: 1px solid rgba(255,255,255,.1); padding: 7px 0; }
        .issue .t { font-weight: 600; text-transform: uppercase; font-size: 10.5px;
                    letter-spacing: .04em; color: #c3c2b7; display: flex; justify-content: space-between; }
        .issue .t .tok { color: #3ecf6e; }
        .issue .d { color: #b5b4ac; margin-top: 3px; line-height: 1.4; }
        .row { display: flex; gap: 8px; margin-top: 10px; }
        button {
          flex: 1; background: #3987e5; color: #fff; border: none; border-radius: 7px;
          font: inherit; font-weight: 600; padding: 7px 0; cursor: pointer;
        }
        button.ghost { background: none; border: 1px solid rgba(255,255,255,.25); color: #e8e8e4; }
        button:disabled { opacity: .45; cursor: default; }
        .foot { color: #8a8983; font-size: 10.5px; margin-top: 8px; line-height: 1.4; }
      </style>
      <div class="chip"><span class="dot"></span><span class="txt"></span></div>
      <div class="panel"></div>`;
    chip = root.querySelector(".chip");
    panel = root.querySelector(".panel");
    chip.addEventListener("click", () => {
      if (panel.classList.contains("show")) closePanel(true);
      else openPanel();
    });
    document.addEventListener("click", e => {
      if (!panel || !panel.classList.contains("show")) return;
      if (e.composedPath().includes(host)) return;
      closePanel(true);
    }, true);
    document.body.appendChild(host);
  }

  function openPanel() {
    panel.style.display = "block";
    void panel.offsetHeight; // reflow so the opacity transition still plays
    panel.classList.add("show");
    position();
  }
  function closePanel(byUser) {
    panel.classList.remove("show");
    panel.style.display = "none";
    if (byUser) manuallyClosed = true;
  }

  /* ---- editable-root resolution (SPA-proof) ---- */
  const editableRoot = node => {
    if (!node) return null;
    const el = node.nodeType === 1 ? node : node.parentElement;
    if (!el || !el.closest) return null;
    return el.closest('textarea, [contenteditable="true"]');
  };

  const readText = el => el.tagName === "TEXTAREA" ? el.value : (el.innerText || "");

  function writeText(el, text) {
    if (el.tagName === "TEXTAREA") {
      el.value = text;
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      el.focus();
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
      if (!document.execCommand("insertText", false, text)) {
        el.innerText = text;
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  }

  function position() {
    if (!target || !chip) return;
    if (!target.isConnected) { hide(); return; }
    const r = target.getBoundingClientRect();
    if (!r.width && !r.height) { hide(); return; }
    const cw = chip.offsetWidth || 160;
    let x = Math.min(r.right - cw - 6, window.innerWidth - cw - 10);
    let y = r.top - 34;
    if (y < 6) y = r.bottom + 8;
    chip.style.left = Math.max(x, 6) + "px";
    chip.style.top = y + "px";
    if (panel.classList.contains("show")) {
      const ph = panel.offsetHeight || 300;
      let py = y - ph - 8;
      if (py < 6) py = Math.min(y + 34, window.innerHeight - ph - 8);
      panel.style.left = Math.max(Math.min(x, window.innerWidth - 350), 6) + "px";
      panel.style.top = Math.max(py, 6) + "px";
    }
  }

  function hide() {
    if (!chip) return;
    chip.classList.remove("show");
    chip.style.display = "none";
    closePanel(false);
  }

  /* subtle busy state: keep the last result visible, pulse the dot;
   * only say "analyzing…" when there is no prior result to show */
  function showBusy() {
    ensureUi();
    chip.classList.add("busy");
    if (chip.style.display !== "flex") {
      chip.className = "chip busy";
      chip.style.display = "flex";
      void chip.offsetHeight;
      chip.classList.add("show");
      root.querySelector(".txt").textContent = "analyzing…";
      position();
    }
  }

  function render(a, rawText) {
    lastAnalysis = a;
    ensureUi();
    chip.classList.remove("busy");
    if (!a || a.originalTokens < SHOW_TOKENS) { hide(); return; }

    const hasFindings = a.issues.length && a.originalTokens >= MIN_TOKENS;
    const cls = !hasFindings ? "" :
      a.reductionPct >= 30 ? "bad" : a.reductionPct >= 12 ? "warn" : "";
    chip.className = "chip show " + cls;
    chip.style.display = "flex";
    root.querySelector(".txt").textContent = hasFindings
      ? `~${a.originalTokens} tok · ${a.issues.length} issue${a.issues.length > 1 ? "s" : ""} · −${a.reductionPct}%`
      : `~${a.originalTokens} tok · looks lean ✓`;

    if (!hasFindings) { closePanel(false); position(); return; }

    const fixable = a.issues.some(i => i.fixable && i.kind === "input");
    const needsConstraint = a.issues.some(i => i.type === "no_output_constraint");
    // dry-run both rewrites so the buttons promise exactly what they deliver
    let deepAfter = null, quickAfter = null;
    if (rawText && rawText.length < 40000) {
      try {
        deepAfter = Coach.deepClean(rawText).after;
        quickAfter = Coach.quickFix(rawText).after;
      } catch {}
    }
    panel.innerHTML = `
      <h3><span>Prompt coach</span>
        <span class="save">~${a.originalTokens} tokens now</span></h3>
      ${a.issues.map(i => `
        <div class="issue">
          <div class="t"><span>${i.type.replace(/_/g, " ")}</span>
            <span class="tok">−${i.tokensSaved}${i.kind === "output" ? " out" : ""} tok</span></div>
          <div class="d">${i.detail}</div>
        </div>`).join("")}
      <div class="row">
        <button class="deep">Deep clean${deepAfter ? ` → ~${deepAfter}` : ""}</button>
        <button class="ghost fix" ${fixable ? "" : "disabled"}>Quick fix${quickAfter && fixable ? ` → ~${quickAfter}` : ""}</button>
        <button class="ghost constraint" ${needsConstraint ? "" : "disabled"}>+ constraint</button>
      </div>
      <div class="foot">Deep clean rewrites aggressively (Ctrl+Z undoes). Code, quotes and
        backticks are never touched. Add specifics yourself — what data, what output
        format. All local. TokenTracker</div>`;
    panel.querySelector(".fix").onclick = applyFix;
    panel.querySelector(".deep").onclick = applyDeepClean;
    panel.querySelector(".constraint").onclick = applyConstraint;

    // a materially different set of findings resets the auto-open suppression
    const key = a.issues.map(i => i.type).sort().join(",");
    if (key !== autoKey) { autoKey = key; manuallyClosed = false; }
    position();
  }

  async function bumpStats(tokensSaved) {
    const s = await storage.get({ fixesApplied: 0, tokensSaved: 0 });
    await storage.set({
      fixesApplied: s.fixesApplied + 1,
      tokensSaved: s.tokensSaved + tokensSaved,
    });
  }

  function currentTarget() {
    if (target && target.isConnected) return target;
    target = editableRoot(document.activeElement);
    return target;
  }

  function applyFix() {
    const el = currentTarget();
    if (!el) return;
    const fixed = Coach.quickFix(readText(el));
    writeText(el, fixed.text);
    bumpStats(fixed.tokensSaved);
    analyzeNow();
  }

  function applyDeepClean() {
    const el = currentTarget();
    if (!el) return;
    const fixed = Coach.deepClean(readText(el));
    writeText(el, fixed.text);
    bumpStats(fixed.tokensSaved);
    analyzeNow();
  }

  function applyConstraint() {
    const el = currentTarget();
    if (!el) return;
    writeText(el, Coach.addConstraint(readText(el)));
    bumpStats(0);
    analyzeNow();
  }

  function analyzeNow() {
    const el = currentTarget();
    if (!el) { hide(); return; }
    const text = readText(el);
    render(text.trim().length ? Coach.analyze(text) : null, text);
  }

  /* auto-open once per distinct finding-set when the user pauses; a manual
   * close suppresses it until the findings change */
  function autoOpenOnPause() {
    if (manuallyClosed || !lastAnalysis || !lastAnalysis.issues.length) return;
    if (lastAnalysis.originalTokens < MIN_TOKENS) return;
    if (!chip || chip.style.display !== "flex") return;
    if (!panel.classList.contains("show")) openPanel();
  }

  function onActivity(el) {
    if (!enabled) return;
    if (el !== target) { target = el; autoKey = ""; manuallyClosed = false; }
    showBusy();
    clearTimeout(debounceTimer);
    clearTimeout(idleTimer);
    debounceTimer = setTimeout(analyzeNow, DEBOUNCE_MS);
    idleTimer = setTimeout(autoOpenOnPause, IDLE_MS);
  }

  // one delegated listener; never trusts a stored element across re-renders
  document.addEventListener("input", e => {
    const el = editableRoot(e.composedPath ? e.composedPath()[0] : e.target);
    if (el) onActivity(el);
  }, true);

  document.addEventListener("focusin", e => {
    const el = editableRoot(e.target);
    if (!el || el === target) return;
    target = el; autoKey = ""; manuallyClosed = false;
    if (enabled) analyzeNow();
  });

  addEventListener("scroll", position, true);
  addEventListener("resize", position);
})();
