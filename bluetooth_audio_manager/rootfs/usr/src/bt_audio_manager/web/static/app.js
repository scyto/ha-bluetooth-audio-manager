/**
 * Bluetooth Audio Manager — Ingress UI
 *
 * Simple vanilla JS interface for device management.
 * Communicates with the add-on's REST API.
 *
 * Uses WebSocket for real-time updates.  SSE is broken through
 * HA ingress due to a compression bug (supervisor#6470).
 * WebSocket bypasses both the bug and the HA service worker.
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

let errorDismissTimer = null;

function showStatus(text) {
  clearTimeout(errorDismissTimer);
  const bar = $("#scan-status");
  const label = $("#scan-status-text");
  const spinner = bar.querySelector(".spinner");
  label.textContent = text;
  spinner.classList.remove("hidden");
  bar.classList.remove("hidden", "error");
  setButtonsEnabled(false);
}

function hideStatus() {
  clearTimeout(errorDismissTimer);
  const bar = $("#scan-status");
  bar.classList.add("hidden");
  bar.classList.remove("error");
  setButtonsEnabled(true);
}

function showError(message) {
  clearTimeout(errorDismissTimer);
  const bar = $("#scan-status");
  const label = $("#scan-status-text");
  const spinner = bar.querySelector(".spinner");
  label.textContent = message;
  spinner.classList.add("hidden");
  bar.classList.remove("hidden");
  bar.classList.add("error");
  setButtonsEnabled(true);
  errorDismissTimer = setTimeout(hideStatus, 5000);
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

      // Connection detail: bearer type + transport status
      let connDetail = "";
      if (d.connected) {
        const parts = [];
        if (d.bearers && d.bearers.length > 0) {
          parts.push(d.bearers.join(" + "));
        }
        if (d.has_transport) {
          parts.push("A2DP");
        }
        if (parts.length > 0) {
          connDetail = ` (${parts.join(" / ")})`;
        }
      }

      return `
        <div class="device-card">
          <div class="device-info">
            <span class="device-name">${escapeHtml(d.name)}</span>
            <span class="device-status ${statusClass}">${statusText}${connDetail}</span>
            <div class="device-address">${escapeHtml(d.address)}${rssiDisplay}${d.adapter ? ` on ${escapeHtml(d.adapter)}` : ""}</div>
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

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text || "";
  return div.innerHTML;
}

// -- Actions --

async function refreshDevices() {
  try {
    const [devResult, sinkResult] = await Promise.all([
      apiGet("/api/devices"),
      apiGet("/api/audio/sinks"),
    ]);
    renderDevices(devResult.devices);
    renderSinks(sinkResult.sinks);
  } catch (e) {
    showError(`Refresh failed: ${e.message}`);
  }
}

async function scanDevices() {
  // Server broadcasts status via WebSocket ("Scanning...")
  try {
    await apiPost("/api/scan");
  } catch (e) {
    showError(`Scan failed: ${e.message}`);
  }
}

async function pairDevice(address) {
  try {
    await apiPost("/api/pair", { address });
  } catch (e) {
    showError(`Pairing failed: ${e.message}`);
  }
}

async function connectDevice(address) {
  try {
    await apiPost("/api/connect", { address });
  } catch (e) {
    showError(`Connection failed: ${e.message}`);
  }
}

async function disconnectDevice(address) {
  try {
    await apiPost("/api/disconnect", { address });
  } catch (e) {
    showError(`Disconnect failed: ${e.message}`);
  }
}

async function forgetDevice(address) {
  if (!confirm(`Forget device ${address}? This will unpair it.`)) return;
  try {
    await apiPost("/api/forget", { address });
  } catch (e) {
    showError(`Forget failed: ${e.message}`);
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

// -- WebSocket (real-time updates) --

let ws = null;
let wsReconnectDelay = 1000;
const WS_MAX_DELAY = 30000;
const WS_BACKOFF = 1.5;

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${proto}//${location.host}${API_BASE}/api/ws`;
  console.log("[WS] Connecting to", wsUrl);

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log("[WS] Connected");
    wsReconnectDelay = 1000;
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    switch (msg.type) {
      case "devices_changed":
        renderDevices(msg.devices);
        break;
      case "sinks_changed":
        renderSinks(msg.sinks);
        break;
      case "mpris_command":
        appendMprisCommand(msg);
        break;
      case "avrcp_event":
        appendAvrcpEvent(msg);
        break;
      case "status":
        if (msg.message) {
          showStatus(msg.message);
        } else {
          hideStatus();
        }
        break;
      default:
        console.log("[WS] Unknown message type:", msg.type);
    }
  };

  ws.onclose = () => {
    console.log("[WS] Closed, reconnecting in", wsReconnectDelay, "ms");
    ws = null;
    setTimeout(connectWebSocket, wsReconnectDelay);
    wsReconnectDelay = Math.min(wsReconnectDelay * WS_BACKOFF, WS_MAX_DELAY);
  };

  ws.onerror = () => {
    console.warn("[WS] Error");
    ws.close();
  };
}

// -- Adapter rendering --

function renderAdapters(adapters) {
  const list = $("#adapters-list");

  if (!adapters || adapters.length === 0) {
    list.innerHTML =
      '<p class="placeholder">No Bluetooth adapters found.</p>';
    return;
  }

  list.innerHTML = adapters
    .map((a) => {
      const selectedBadge = a.selected
        ? '<span class="device-status status-connected">In Use</span>'
        : "";
      const bleBadge = a.ble_scanning
        ? '<span class="device-status status-paired">HA BLE Scanning</span>'
        : "";
      const poweredLabel = a.powered ? "Powered" : "Off";
      const poweredClass = a.powered ? "status-connected" : "status-discovered";

      // Show model name if available, otherwise just the hci name
      const displayName = a.hw_model
        ? `${escapeHtml(a.name)} — ${escapeHtml(a.hw_model)}`
        : escapeHtml(a.name);

      // Select button for non-selected, powered adapters
      const selectBtn = !a.selected && a.powered
        ? `<button class="btn btn-small btn-primary" onclick="selectAdapter('${a.name}')">Select</button>`
        : "";

      return `
        <div class="sink-card">
          <div>
            <div class="sink-name">${displayName}</div>
            <div class="sink-description">${escapeHtml(a.address)}</div>
          </div>
          <div>
            <span class="device-status ${poweredClass}">${poweredLabel}</span>
            ${selectedBadge}
            ${bleBadge}
            ${selectBtn}
          </div>
        </div>
      `;
    })
    .join("");
}

async function selectAdapter(adapterName) {
  if (!confirm(`Switch to adapter ${adapterName}? The add-on will restart.`)) return;
  try {
    showStatus(`Switching to adapter ${adapterName}...`);
    const result = await apiPost("/api/set-adapter", { adapter: adapterName });
    if (result.restart_required) {
      showStatus("Restarting add-on with new adapter...");
      await apiPost("/api/restart");
    }
  } catch (e) {
    showError(`Adapter switch failed: ${e.message}`);
  }
}

async function loadAdapters() {
  try {
    const data = await apiGet("/api/adapters");
    renderAdapters(data.adapters);
  } catch (e) {
    console.warn("Failed to load adapters:", e.message);
  }
}

// -- Init --

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-scan").addEventListener("click", scanDevices);
  $("#btn-refresh").addEventListener("click", refreshDevices);

  // WebSocket provides real-time updates (initial state sent on connect)
  connectWebSocket();

  // Load adapter info (once at startup)
  loadAdapters();

  // Show version and adapter in footer
  apiGet("/api/info")
    .then((data) => {
      $("#version-label").textContent = `v${data.version} (${data.adapter})`;
    })
    .catch(() => {});
});
