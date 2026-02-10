/**
 * Bluetooth Audio Manager — Ingress UI
 *
 * Simple vanilla JS interface for device management.
 * Communicates with the add-on's REST API.
 *
 * Uses polling instead of SSE because HA's service worker
 * (StrategyHandler.js) intercepts EventSource connections and
 * breaks SSE streaming through the ingress proxy.
 */

// HA ingress serves the page at /api/hassio_ingress/<token>/
// Use the current page path as the base so API calls route through ingress
const API_BASE = document.location.pathname.replace(/\/$/, "");

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// -- API helpers --

async function apiGet(path) {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

async function apiPost(path, body = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => null);
    throw new Error(data?.error || `API error: ${resp.status}`);
  }
  return resp.json();
}

// -- UI state --

function showStatus(text) {
  const bar = $("#scan-status");
  const label = $("#scan-status-text");
  label.textContent = text;
  bar.classList.remove("hidden");
}

function hideStatus() {
  $("#scan-status").classList.add("hidden");
}

function setButtonsEnabled(enabled) {
  $$("#btn-scan, #btn-refresh").forEach((btn) => {
    btn.disabled = !enabled;
  });
}

// -- Bluetooth UUID to profile label mapping --

const BT_PROFILES = {
  "0000110b-0000-1000-8000-00805f9b34fb": "A2DP Sink",
  "0000110a-0000-1000-8000-00805f9b34fb": "A2DP Source",
  "0000110c-0000-1000-8000-00805f9b34fb": "AVRCP Target",
  "0000110e-0000-1000-8000-00805f9b34fb": "AVRCP Controller",
  "0000111e-0000-1000-8000-00805f9b34fb": "HFP",
};

function profileLabels(uuids) {
  if (!uuids || uuids.length === 0) return "";
  const labels = uuids
    .map((u) => BT_PROFILES[u.toLowerCase()])
    .filter(Boolean);
  return labels.length > 0 ? labels.join(", ") : "";
}

// -- Rendering --

function renderDevices(devices) {
  const list = $("#devices-list");

  if (!devices || devices.length === 0) {
    list.innerHTML =
      '<p class="placeholder">No Bluetooth audio devices found. Put your speaker in pairing mode and scan again.</p>';
    return;
  }

  list.innerHTML = devices
    .map((d) => {
      const statusClass = d.connected
        ? "status-connected"
        : d.paired
          ? "status-paired"
          : "status-discovered";
      const statusText = d.connected
        ? "Connected"
        : d.paired
          ? "Paired"
          : "Discovered";

      let actions = "";
      if (d.connected) {
        actions = `
          <button class="btn btn-small btn-danger" onclick="disconnectDevice('${d.address}')">Disconnect</button>
          <button class="btn btn-small btn-danger" onclick="forgetDevice('${d.address}')">Forget</button>
        `;
      } else if (d.paired || d.stored) {
        actions = `
          <button class="btn btn-small btn-success" onclick="connectDevice('${d.address}')">Connect</button>
          <button class="btn btn-small btn-danger" onclick="forgetDevice('${d.address}')">Forget</button>
        `;
      } else {
        actions = `
          <button class="btn btn-small btn-primary" onclick="pairDevice('${d.address}')">Pair</button>
        `;
      }

      const rssiDisplay = d.rssi ? ` (${d.rssi} dBm)` : "";
      const profiles = profileLabels(d.uuids);

      return `
        <div class="device-card">
          <div class="device-info">
            <span class="device-name">${escapeHtml(d.name)}</span>
            <span class="device-status ${statusClass}">${statusText}</span>
            <div class="device-address">${escapeHtml(d.address)}${rssiDisplay}</div>
            ${profiles ? `<div class="device-profiles">${escapeHtml(profiles)}</div>` : ""}
          </div>
          <div class="device-actions">${actions}</div>
        </div>
      `;
    })
    .join("");
}

function renderSinks(sinks) {
  const list = $("#sinks-list");

  if (!sinks || sinks.length === 0) {
    list.innerHTML =
      '<p class="placeholder">No Bluetooth audio sinks available. Connect a device first.</p>';
    return;
  }

  list.innerHTML = sinks
    .map(
      (s) => {
        const stateClass = s.state === "running" ? "status-connected" : "status-paired";
        const stateLabel = s.state.charAt(0).toUpperCase() + s.state.slice(1);
        const vol = s.mute ? "Muted" : `${s.volume}%`;
        const audioInfo = [
          s.sample_rate ? `${(s.sample_rate / 1000).toFixed(1)} kHz` : null,
          s.channels ? `${s.channels}ch` : null,
          s.format || null,
        ].filter(Boolean).join(" / ");

        return `
      <div class="sink-card">
        <div>
          <div class="sink-name">${escapeHtml(s.description || s.name)}</div>
          <div class="sink-description">${escapeHtml(s.name)}</div>
          <div class="sink-details">${audioInfo ? escapeHtml(audioInfo) : ""} ${escapeHtml(vol)}</div>
        </div>
        <div>
          <span class="device-status ${stateClass}">${escapeHtml(stateLabel)}</span>
        </div>
      </div>
    `;
      }
    )
    .join("");
}

function showError(message) {
  const list = $("#devices-list");
  list.innerHTML = `<div class="error-message">${escapeHtml(message)}</div>`;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text || "";
  return div.innerHTML;
}

// -- Actions --

async function refreshDevices() {
  try {
    setButtonsEnabled(false);
    showStatus("Refreshing...");
    const [devResult, sinkResult] = await Promise.all([
      apiGet("/api/devices"),
      apiGet("/api/audio/sinks"),
    ]);
    renderDevices(devResult.devices);
    renderSinks(sinkResult.sinks);
  } catch (e) {
    showError(`Refresh failed: ${e.message}`);
  } finally {
    hideStatus();
    setButtonsEnabled(true);
  }
}

async function scanDevices() {
  try {
    setButtonsEnabled(false);
    showStatus("Scanning for Bluetooth audio devices...");
    await apiPost("/api/scan");
  } catch (e) {
    showError(`Scan failed: ${e.message}`);
  } finally {
    hideStatus();
    setButtonsEnabled(true);
  }
}

async function pairDevice(address) {
  try {
    showStatus(`Pairing with ${address}...`);
    await apiPost("/api/pair", { address });
  } catch (e) {
    showError(`Pairing failed: ${e.message}`);
  } finally {
    hideStatus();
  }
}

async function connectDevice(address) {
  try {
    showStatus(`Connecting to ${address}...`);
    await apiPost("/api/connect", { address });
  } catch (e) {
    showError(`Connection failed: ${e.message}`);
  } finally {
    hideStatus();
  }
}

async function disconnectDevice(address) {
  try {
    showStatus(`Disconnecting ${address}...`);
    await apiPost("/api/disconnect", { address });
  } catch (e) {
    showError(`Disconnect failed: ${e.message}`);
  } finally {
    hideStatus();
  }
}

async function forgetDevice(address) {
  if (!confirm(`Forget device ${address}? This will unpair it.`)) return;
  try {
    showStatus(`Forgetting ${address}...`);
    await apiPost("/api/forget", { address });
  } catch (e) {
    showError(`Forget failed: ${e.message}`);
  } finally {
    hideStatus();
  }
}

// -- Event log helpers --

const MAX_LOG_ENTRIES = 50;

function appendLogEntry(logSelector, cssClass, html) {
  const log = $(logSelector);

  // Remove placeholder if present
  const placeholder = log.querySelector(".placeholder");
  if (placeholder) placeholder.remove();

  const entry = document.createElement("div");
  entry.className = cssClass;
  entry.innerHTML = html;

  log.appendChild(entry);

  // Trim old entries
  while (log.children.length > MAX_LOG_ENTRIES) {
    log.removeChild(log.firstChild);
  }

  // Auto-scroll to bottom
  log.scrollTop = log.scrollHeight;
}

function eventTime(data) {
  // Use server timestamp if available, otherwise current time
  const d = data.ts ? new Date(data.ts * 1000) : new Date();
  return d.toLocaleTimeString();
}

function appendMprisCommand(data) {
  appendLogEntry(
    "#mpris-log",
    "log-entry",
    `<span class="log-time">${escapeHtml(eventTime(data))}</span>`
    + `<span class="log-command">${escapeHtml(data.command)}</span>`
    + (data.detail ? ` <span class="log-detail">${escapeHtml(data.detail)}</span>` : ""),
  );
}

function appendAvrcpEvent(data) {
  const valueStr = typeof data.value === "object"
    ? JSON.stringify(data.value)
    : String(data.value);

  appendLogEntry(
    "#avrcp-log",
    "log-entry",
    `<span class="log-time">${escapeHtml(eventTime(data))}</span>`
    + `<span class="log-prop">${escapeHtml(data.property)}</span> = `
    + `<span class="log-value">${escapeHtml(valueStr)}</span>`,
  );
}

// -- Polling (replaces SSE which is broken by HA's service worker) --

let pollTimer = null;
let lastMprisTs = 0;
let lastAvrcpTs = 0;

async function pollState() {
  try {
    const params = new URLSearchParams({
      mpris_after: lastMprisTs,
      avrcp_after: lastAvrcpTs,
    });
    const data = await apiGet(`/api/state?${params}`);

    renderDevices(data.devices);
    renderSinks(data.sinks);

    // Append only new events
    for (const ev of data.mpris_events) {
      appendMprisCommand(ev);
      if (ev.ts > lastMprisTs) lastMprisTs = ev.ts;
    }
    for (const ev of data.avrcp_events) {
      appendAvrcpEvent(ev);
      if (ev.ts > lastAvrcpTs) lastAvrcpTs = ev.ts;
    }
  } catch (e) {
    console.warn("[Poll] Error:", e.message);
  }
}

function startPolling() {
  if (pollTimer) return;
  console.log("[Poll] Starting (3s interval)");
  pollState();
  pollTimer = setInterval(pollState, 3000);
}

// -- Init --

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-scan").addEventListener("click", scanDevices);
  $("#btn-refresh").addEventListener("click", refreshDevices);

  // Poll for state updates every 3 seconds
  startPolling();

  // Show version in footer
  apiGet("/api/info")
    .then((data) => {
      $("#version-label").textContent = `v${data.version}`;
    })
    .catch(() => {});
});
