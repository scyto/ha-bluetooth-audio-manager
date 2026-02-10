/**
 * Bluetooth Audio Manager — Ingress UI
 *
 * Simple vanilla JS interface for device management.
 * Communicates with the add-on's REST API.
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
    // SSE will push status + results as they arrive
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
    await apiPost("/api/pair", { address });
    // SSE pushes updated device list
  } catch (e) {
    showError(`Pairing failed: ${e.message}`);
    hideStatus();
  }
}

async function connectDevice(address) {
  try {
    await apiPost("/api/connect", { address });
    // SSE pushes status progress + updated state
  } catch (e) {
    showError(`Connection failed: ${e.message}`);
    hideStatus();
  }
}

async function disconnectDevice(address) {
  try {
    await apiPost("/api/disconnect", { address });
    // SSE pushes updated state
  } catch (e) {
    showError(`Disconnect failed: ${e.message}`);
    hideStatus();
  }
}

async function forgetDevice(address) {
  if (!confirm(`Forget device ${address}? This will unpair it.`)) return;
  try {
    await apiPost("/api/forget", { address });
    // SSE pushes updated state
  } catch (e) {
    showError(`Forget failed: ${e.message}`);
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

function appendMprisCommand(data) {
  const time = new Date().toLocaleTimeString();
  appendLogEntry(
    "#mpris-log",
    "log-entry",
    `<span class="log-time">${escapeHtml(time)}</span>`
    + `<span class="log-command">${escapeHtml(data.command)}</span>`
    + (data.detail ? ` <span class="log-detail">${escapeHtml(data.detail)}</span>` : ""),
  );
}

function appendAvrcpEvent(data) {
  const time = new Date().toLocaleTimeString();
  const valueStr = typeof data.value === "object"
    ? JSON.stringify(data.value)
    : String(data.value);

  appendLogEntry(
    "#avrcp-log",
    "log-entry",
    `<span class="log-time">${escapeHtml(time)}</span>`
    + `<span class="log-prop">${escapeHtml(data.property)}</span> = `
    + `<span class="log-value">${escapeHtml(valueStr)}</span>`,
  );
}

// -- Server-Sent Events (real-time updates) --

let eventSource = null;

function connectSSE() {
  if (eventSource) {
    eventSource.close();
  }

  eventSource = new EventSource(`${API_BASE}/api/events`);

  eventSource.addEventListener("devices_changed", (e) => {
    const data = JSON.parse(e.data);
    renderDevices(data.devices);
  });

  eventSource.addEventListener("sinks_changed", (e) => {
    const data = JSON.parse(e.data);
    renderSinks(data.sinks);
  });

  eventSource.addEventListener("status", (e) => {
    const data = JSON.parse(e.data);
    if (data.message) {
      showStatus(data.message);
    } else {
      hideStatus();
    }
  });

  eventSource.addEventListener("mpris_command", (e) => {
    const data = JSON.parse(e.data);
    appendMprisCommand(data);
  });

  eventSource.addEventListener("avrcp_event", (e) => {
    const data = JSON.parse(e.data);
    appendAvrcpEvent(data);
  });

  eventSource.onerror = () => {
    // EventSource auto-reconnects after a few seconds
  };
}

// -- Init --

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-scan").addEventListener("click", scanDevices);
  $("#btn-refresh").addEventListener("click", refreshDevices);

  // SSE provides real-time updates (and sends initial state on connect)
  connectSSE();

  // Also fetch initial state via REST as a fallback — SSE initial
  // data may be delayed by ingress proxy buffering
  refreshDevices();

  // Show version in footer
  apiGet("/api/info")
    .then((data) => {
      $("#version-label").textContent = `v${data.version}`;
    })
    .catch(() => {});
});
