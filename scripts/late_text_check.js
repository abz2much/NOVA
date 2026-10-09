/**
 * Text the panel draws after its first render is translated too (8.17.0).
 *
 * Loads a fake language ("xx") and checks one fixed string in each group of
 * places that set text later: logs, memory, faces, intrusion, status
 * messages (the panel's toasts) and browser dialogs.
 *
 * Run:  NODE_PATH=node_modules node scripts/late_text_check.js
 */
const { window, hass } = require("./smoke_panel.js");

const FAKE = {
  "Memory": "XX Memory",   // control: drawn at the first render, so it works already
  "No spoken messages recorded yet.": "XX no spoken messages",
  "Waiting for confirmation": "XX waiting",
  "No residents yet. Add the people who live here.": "XX no residents",
  "✕ CALL OFF (FALSE ALARM)": "✕ XX CALL OFF",
  "Could not add that name.": "XX could not add",
  "Forget everything scene memory has kept?": "XX forget everything?",
};

const realFetch = global.fetch;
const fakeFetch = async url => (/\/i18n\/xx\.json$/.test(String(url))
  ? { ok: true, json: async () => FAKE }
  : realFetch(url));
global.fetch = window.fetch = fakeFetch;
hass.language = "xx";

const wait = ms => new Promise(r => setTimeout(r, ms));
const text = root => root.textContent || "";

(async () => {
  const checks = [];
  const el = window.document.createElement("nova-panel");
  window.document.body.appendChild(el);
  el.hass = hass;
  await wait(250);
  const root = el.shadowRoot;
  checks.push(["control: the fake language loaded (a first render label is translated)",
    [...root.querySelectorAll(".nav-tab")].some(t => t.textContent.trim() === "XX Memory")]);
  const tab = async name => { root.querySelector(`.nav-tab[data-tab="${name}"]`).click(); await wait(400); };

  // Logs: the spoken history list is drawn after its data arrives.
  await tab("logs");
  let box = root.getElementById("spokenHistoryEntries");
  if (!box) { box = window.document.createElement("div"); box.id = "spokenHistoryEntries"; root.appendChild(box); }
  el._spokenHistory = [];
  el._renderSpokenHistoryRows();
  checks.push(["logs: late drawn list text is translated",
    text(root.getElementById("spokenHistoryEntries")).includes("XX no spoken messages")]);

  await tab("memory");
  checks.push(["memory: late drawn pending facts are translated", text(root).includes("XX waiting")]);

  await tab("faces");
  checks.push(["faces: late drawn resident list is translated", text(root).includes("XX no residents")]);

  await tab("intrusion");
  checks.push(["intrusion: late drawn status buttons are translated", text(root).includes("✕ XX CALL OFF")]);

  // Status messages: the faces tab's message after a failed add.
  await tab("faces");
  const realCallWS = hass.callWS;
  hass.callWS = async msg => { if (msg.type === "nova/add_resident") throw new Error(""); return realCallWS(msg); };
  await el._faceAdd("Sam");
  hass.callWS = realCallWS;
  checks.push(["status message: a late message is translated",
    (root.getElementById("facesMsg")?.textContent || "") === "XX could not add"]);

  // Dialogs: the message a confirm() shows.
  await tab("settings");
  let asked = null;
  const realConfirm = window.confirm;
  window.confirm = message => { asked = message; return false; };
  root.getElementById("sceneMemoryClear")?.click();
  window.confirm = realConfirm;
  checks.push(["dialog: a confirm message is translated", asked === "XX forget everything?"]);

  let ok = true;
  for (const [name, pass] of checks) { console.log((pass ? "  PASS  " : "  FAIL  ") + name); if (!pass) ok = false; }
  console.log(ok ? "\nLATE TEXT CHECK CLEAN" : "\nLATE TEXT CHECK FAILED");
  process.exit(ok ? 0 : 1);
})();
