/**
 * The list of fixed English strings the panel shows, for the translation checks.
 *
 * Two sources, merged:
 *   1. A render. Every tab is drawn under jsdom with the smoke test's data, and
 *      each text node and title/placeholder attribute is read exactly as
 *      _localizeDOM sees it (trimmed). Only text that is written literally in
 *      frontend/src counts, so live data (area names, entity ids) is left out.
 *   2. A scan of frontend/src for text the render cannot reach: dialogs, errors,
 *      empty states, the other side of a ternary, and every _t("...") template.
 *
 * Run:
 *   NODE_PATH=node_modules node scripts/panel_strings.js --check   # CI: fail if the file is stale
 *   NODE_PATH=node_modules node scripts/panel_strings.js --write   # update the file
 *
 * tests/unit/test_panel_i18n.py reads the file and checks every language.
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const SRC = path.join(ROOT, "frontend", "src");
const OUT = path.join(ROOT, "frontend", "i18n", "panel_strings.json");

// Technical values are never translated (see custom_components/nova/frontend/i18n/README.md).
const TECHNICAL = [
  /^[a-z_]+\.[a-z0-9_*]+$/,            // entity ids and patterns
  /^https?:\/\//,                      // URLs
];

function wanted(s) {
  if (!s || !/[A-Za-z]{2}/.test(s)) return false;
  return !TECHNICAL.some(re => re.test(s));
}

// A quoted literal in code is only taken as text when it reads like text. A
// lone lower case word here is far more often an id than a label; when one is
// really shown, the render finds it.
const NOT_TEXT = [
  /\n/, /^[.#/]/, /px\b/, /var\(/, /;/, /=>/, /\(\)/,
  /^[a-z0-9_.\/-]+$/,                  // ids, config keys, websocket commands
  /^\S*\/\S*$/,                        // provider/model, MIME types
  /^[a-z]+[A-Z]\w*$/, /^[A-Z][a-z]+[A-Z]\w*$/,   // code identifiers
  /^(?:rgba?|hsla?)\(/,                // colours
];
const looksLikeText = s => wanted(s) && s === s.trim() && !NOT_TEXT.some(re => re.test(s));

function sourceFiles() {
  const out = [];
  const walk = dir => fs.readdirSync(dir, { withFileTypes: true }).forEach(e => {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (e.name.endsWith(".js")) out.push(p);
  });
  walk(SRC);
  return out.sort();
}

// Every double quoted string in the first argument of _t(...) or _tHtml(...),
// so both sides of `n === 1 ? "{count} sensor" : "{count} sensors"` count.
function templateKeys(src) {
  const keys = [];
  for (const m of src.matchAll(/\b_t(?:Html)?\(/g)) {
    let depth = 0;
    for (let i = m.index + m[0].length; i < src.length; i++) {
      const c = src[i];
      if (c === '"') {
        let j = i + 1;
        while (j < src.length && src[j] !== '"') j += src[j] === "\\" ? 2 : 1;
        keys.push(JSON.parse(src.slice(i, j + 1)));
        i = j;
      } else if (c === "(" || c === "[" || c === "{") depth++;
      else if (c === ")" || c === "]" || c === "}") { if (depth === 0) break; depth--; }
      else if (c === "," && depth === 0) break;
    }
  }
  return keys;
}

function scanSource(decode) {
  const found = new Set();
  const add = s => { s = decode(s).trim(); if (wanted(s)) found.add(s); };
  for (const file of sourceFiles()) {
    if (path.basename(file) === "styles.js") continue;
    const src = fs.readFileSync(file, "utf8");
    // Text between two tags, on one line, with no value inside it.
    for (const m of src.matchAll(/<[a-zA-Z][^<>]*>([^<>\n]+)<\/?[a-zA-Z]/g)) {
      const seg = m[1];
      if (seg.includes("${")) {
        // A value decides between fixed words: keep both words.
        for (const t of seg.matchAll(/\?\s*"([^"\\$]*)"\s*:\s*"([^"\\$]*)"/g)) { add(t[1]); add(t[2]); }
        continue;
      }
      if (/[`{};]|=>|&&|\|\|/.test(seg)) continue;
      add(seg);
    }
    for (const m of src.matchAll(/\b(?:title|placeholder)="([^"$]*)"/g)) add(m[1]);
    // Labels kept in tables: option pairs ["key", "Label"] and maps { key: "Label" }.
    const addText = s => { if (looksLikeText(s)) found.add(s); };
    for (const m of src.matchAll(/\[\s*"[^"\n]*"\s*,\s*"([^"\\$\n]+)"/g)) addText(m[1]);
    for (const m of src.matchAll(/\b[\w$]+\s*:\s*"([^"\\$\n]+)"/g)) addText(m[1]);
    // Both sides of a ternary between two pieces of text, wherever it sits.
    for (const m of src.matchAll(/\?\s*"([^"\\$\n]+)"\s*:\s*"([^"\\$\n]+)"/g)) { addText(m[1]); addText(m[2]); }
    // Text set after the render: textContent, toasts and browser dialogs.
    for (const line of src.split("\n")) {
      if (!/\.(?:textContent|innerText)\s*=|_toast\(|_flash\(|\balert\(|\bconfirm\(/.test(line)) continue;
      for (const m of line.matchAll(/"([^"\\$]+)"/g)) addText(m[1]);
    }
    templateKeys(src).forEach(k => found.add(k));
  }
  return found;
}

async function renderStrings(window, hass, isLiteral) {
  const found = new Set();
  const el = window.document.createElement("nova-panel");
  window.document.body.appendChild(el);
  el.hass = hass;
  await new Promise(r => setTimeout(r, 150));
  const collect = root => {
    const walker = window.document.createTreeWalker(root, 4, null);
    let n;
    while ((n = walker.nextNode())) {
      const parent = n.parentNode && n.parentNode.nodeName;
      if (parent === "STYLE" || parent === "SCRIPT") continue;
      const k = (n.nodeValue || "").trim();
      if (wanted(k) && isLiteral(k)) found.add(k);
    }
    root.querySelectorAll("[title],[placeholder]").forEach(e => ["title", "placeholder"].forEach(a => {
      const k = (e.getAttribute(a) || "").trim();
      if (wanted(k) && isLiteral(k)) found.add(k);
    }));
  };
  const tabs = Array.from(el.shadowRoot.querySelectorAll(".nav-tab")).map(b => b.getAttribute("data-tab"));
  for (const tab of tabs) {
    Array.from(el.shadowRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === tab).click();
    await new Promise(r => setTimeout(r, 150));
    collect(el.shadowRoot);
  }
  el.remove();
  return found;
}

async function build() {
  const { window, hass } = require("./smoke_panel.js");
  const box = window.document.createElement("div");
  const decode = s => { box.innerHTML = s; return box.textContent; };
  const raw = sourceFiles().map(f => fs.readFileSync(f, "utf8")).join("\n");
  const decoded = decode(raw.replace(/</g, "&lt;"));
  const isLiteral = s => raw.includes(s) || decoded.includes(s)
    || raw.includes(JSON.stringify(s).slice(1, -1));
  const all = new Set([...scanSource(decode), ...await renderStrings(window, hass, isLiteral)]);
  return [...all].sort();
}

(async () => {
  const mode = process.argv[2];
  if (mode !== "--check" && mode !== "--write") {
    console.error("usage: panel_strings.js --check | --write");
    process.exit(2);
  }
  const strings = await build();
  const text = JSON.stringify(strings, null, 1) + "\n";
  if (mode === "--write") {
    fs.writeFileSync(OUT, text);
    console.log(`wrote ${strings.length} strings to ${path.relative(ROOT, OUT)}`);
    process.exit(0);
  }
  const saved = fs.existsSync(OUT) ? JSON.parse(fs.readFileSync(OUT, "utf8")) : [];
  const added = strings.filter(s => !saved.includes(s));
  const gone = saved.filter(s => !strings.includes(s));
  if (!added.length && !gone.length) {
    console.log(`PANEL STRINGS CURRENT (${strings.length})`);
    process.exit(0);
  }
  added.forEach(s => console.log(`  NEW   ${JSON.stringify(s)}`));
  gone.forEach(s => console.log(`  GONE  ${JSON.stringify(s)}`));
  console.log("\nPANEL STRINGS STALE. Run: NODE_PATH=node_modules node scripts/panel_strings.js --write");
  console.log("Then add the new strings to every file in custom_components/nova/frontend/i18n/.");
  process.exit(1);
})();
