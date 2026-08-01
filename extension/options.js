const DEFAULTS = {
  enabled: true, showBar: true, monthlyBudgetTokens: 1000000,
  tenantUrl: "", employeeToken: "",
  fixesApplied: 0, tokensSaved: 0, ttUsage: {},
};
const monthKey = () => new Date().toISOString().slice(0, 7);
const fmt = n => n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n);

function load() {
  chrome.storage.local.get(DEFAULTS).then(s => {
    document.getElementById("enabled").checked = s.enabled;
    document.getElementById("showBar").checked = s.showBar;
    document.getElementById("budget").value = s.monthlyBudgetTokens;
    document.getElementById("tenantUrl").value = s.tenantUrl;
    document.getElementById("employeeToken").value = s.employeeToken;
    document.getElementById("fixes").textContent = (s.fixesApplied || 0).toLocaleString();
    document.getElementById("tokens").textContent = (s.tokensSaved || 0).toLocaleString();
    const m = (s.ttUsage || {})[monthKey()] || { input: 0, output: 0, messages: 0 };
    document.getElementById("mtd").textContent = fmt(m.input + m.output);
    document.getElementById("msgs").textContent = (m.messages || 0).toLocaleString();
  });
}
load();

// live toggles save immediately
document.getElementById("enabled").addEventListener("change", e =>
  chrome.storage.local.set({ enabled: e.target.checked }));
document.getElementById("showBar").addEventListener("change", e =>
  chrome.storage.local.set({ showBar: e.target.checked }));

document.getElementById("save").addEventListener("click", () => {
  chrome.storage.local.set({
    monthlyBudgetTokens: Math.max(+document.getElementById("budget").value || 1000000, 1000),
    tenantUrl: document.getElementById("tenantUrl").value.trim(),
    employeeToken: document.getElementById("employeeToken").value.trim(),
  }).then(() => {
    const msg = document.getElementById("savedMsg");
    msg.textContent = "saved ✓";
    setTimeout(() => (msg.textContent = ""), 1500);
  });
});

document.getElementById("reset").addEventListener("click", () => {
  chrome.storage.local.set({ fixesApplied: 0, tokensSaved: 0 }).then(load);
});
