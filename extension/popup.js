const monthKey = () => new Date().toISOString().slice(0, 7);
const fmt = n => n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n);

chrome.storage.local.get({
  fixesApplied: 0, tokensSaved: 0, enabled: true, showBar: true,
  monthlyBudgetTokens: 1000000, ttUsage: {},
}).then(s => {
  const m = (s.ttUsage || {})[monthKey()] || { input: 0, output: 0, messages: 0 };
  const used = m.input + m.output;
  const budget = Math.max(s.monthlyBudgetTokens, 1);
  const pct = Math.min(used / budget * 100, 100);
  const color = pct >= 100 ? "#e66767" : pct >= 80 ? "#fab219" : "#3ecf6e";
  document.getElementById("mtd").textContent = fmt(used) + " / " + fmt(budget);
  const fill = document.getElementById("fill");
  fill.style.width = pct + "%"; fill.style.background = color;
  document.getElementById("pct").textContent = Math.round(pct) + "%";
  document.getElementById("left").textContent = fmt(Math.max(budget - used, 0)) + " left";
  document.getElementById("msgs").textContent = (m.messages || 0).toLocaleString();
  document.getElementById("fixes").textContent = (s.fixesApplied || 0).toLocaleString();
  document.getElementById("saved").textContent = (s.tokensSaved || 0).toLocaleString();
  document.getElementById("enabled").checked = s.enabled;
  document.getElementById("showBar").checked = s.showBar;
});

document.getElementById("enabled").addEventListener("change", e =>
  chrome.storage.local.set({ enabled: e.target.checked }));
document.getElementById("showBar").addEventListener("change", e =>
  chrome.storage.local.set({ showBar: e.target.checked }));
document.getElementById("opts").addEventListener("click", e => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});
