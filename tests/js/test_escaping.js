/**
 * HTML-escaping regression tests for web/static/app.js.
 *
 * Device names and adapter aliases come straight from Bluetooth
 * advertisements, so anything in radio range can choose them. app.js builds
 * markup with template literals and assigns it to innerHTML, which means every
 * interpolation of device-derived data is an injection site.
 *
 * There is no JS toolchain in this repo, so this runs on bare node with no
 * dependencies: it extracts the helpers and the card builder out of app.js by
 * name, stubs the one DOM API escapeHtml() needs, and renders cards from
 * hostile input.
 *
 * Run: node tests/js/test_escaping.js
 */

const fs = require("fs");
const path = require("path");

const APP_JS = path.join(__dirname, "..", "..", "src", "bt_audio_manager",
                         "web", "static", "app.js");

// escapeHtml() sets textContent and reads back innerHTML. Per the HTML
// fragment-serialisation spec that escapes & < > and U+00A0 — and nothing
// else. Quotes in particular survive, which is exactly why escapeHtml() is
// not sufficient inside a quoted attribute.
global.document = {
  createElement: () => ({
    _t: "",
    set textContent(v) { this._t = String(v); },
    get textContent() { return this._t; },
    get innerHTML() {
      return this._t
        .replace(/&/g, "&amp;")
        .replace(/ /g, "&nbsp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    },
  }),
};
global.window = {};

const src = fs.readFileSync(APP_JS, "utf8");
for (const name of ["escapeHtml", "escapeAttr", "safeJsString",
                    "buildFeatureBadges", "buildDeviceCard"]) {
  const m = src.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n\\}`));
  if (!m) {
    console.error(`could not extract ${name}() from app.js — did it get renamed?`);
    process.exit(2);
  }
  global[name] = eval("(" + m[0] + ")");
}

// Collaborators buildDeviceCard() calls that are not under test here.
global.profileLabels = () => "A2DP Sink";
global.buildCapBadges = () => "";
global.currentSinks = [];

let failures = 0;
function check(ok, name, detail) {
  if (!ok) failures++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}${ok || !detail ? "" : "  — " + detail}`);
}

// -- Helper contracts --------------------------------------------------------

check(escapeHtml('a"b').includes('"'), "escapeHtml leaves quotes (text-position only)");
check(escapeHtml("<b>") === "&lt;b&gt;", "escapeHtml escapes angle brackets");
check(!escapeAttr('a"b').includes('"'), "escapeAttr escapes double quotes");
check(!escapeAttr("a'b").includes("'"), "escapeAttr escapes single quotes");
check(escapeAttr("a&b") === "a&amp;b", "escapeAttr escapes & first (no double-encoding)");
check(escapeAttr(null) === "" && escapeAttr(undefined) === "", "escapeAttr handles null/undefined");

// mpd_port is `integer, nullable` in openapi.yaml, so the JS-string escaper
// receives numbers. String.prototype.replace on a number throws, which would
// take out the whole device grid via renderDevices()'s map().
check(safeJsString(6600) === "6600", "safeJsString coerces numbers (mpd_port)");
check(safeJsString(null) === "", "safeJsString handles null");

// -- Rendering under hostile input -------------------------------------------

const PAYLOADS = [
  'x" onmouseover="alert(1)',           // break out of a double-quoted attribute
  "x' onfocus='alert(1)",               // break out of a single-quoted attribute
  "</h5><img src=x onerror=alert(1)>",  // close the tag and inject an element
  'x\\" onload=\\"alert(1)',            // backslash-escaped quote
  "'); alert(1); ('",                   // break out of the onclick JS string
];

function realAttributeNames(html) {
  // Attribute names are whatever survives stripping every quoted value from a
  // tag — anything inside quotes is data, not markup.
  const attrs = new Set();
  for (const tag of html.match(/<[^>]*>/g) || []) {
    const stripped = tag.replace(/"[^"]*"/g, '""').replace(/'[^']*'/g, "''");
    for (const m of stripped.matchAll(/[\s(]([a-zA-Z][a-zA-Z0-9-]*)\s*=/g)) {
      attrs.add(m[1].toLowerCase());
    }
  }
  return attrs;
}

const DANGEROUS_TAGS = ["img", "script", "iframe", "svg", "object", "embed"];

for (const payload of PAYLOADS) {
  const device = {
    name: payload, address: "AA:BB:CC:DD:EE:FF", adapter: payload,
    connected: true, paired: true, stored: true,
    uuids: ["0000110b-0000-1000-8000-00805f9b34fb"],
    rssi: -55, signal_quality: payload,
    audio_profile: payload, idle_mode: payload, keep_alive_method: payload,
    mpd_enabled: true, mpd_port: 6600, mpd_hw_volume: 100,
    power_save_delay: 0, auto_disconnect_minutes: 30, avrcp_enabled: true,
  };

  let html;
  try {
    html = buildDeviceCard(device);
  } catch (e) {
    check(false, `renders card for ${JSON.stringify(payload)}`, e.message);
    continue;
  }

  const injectedAttrs = [...realAttributeNames(html)]
    .filter((a) => a.startsWith("on") && a !== "onclick");
  const injectedTags = (html.match(/<([a-zA-Z][a-zA-Z0-9-]*)/g) || [])
    .map((t) => t.slice(1).toLowerCase())
    .filter((t) => DANGEROUS_TAGS.includes(t));

  check(injectedAttrs.length === 0 && injectedTags.length === 0,
        `no injection for ${JSON.stringify(payload)}`,
        `attrs=[${injectedAttrs}] tags=[${injectedTags}]`);
}

console.log(failures ? `\n${failures} failing` : "\nall escaping checks passed");
process.exit(failures ? 1 : 0);
