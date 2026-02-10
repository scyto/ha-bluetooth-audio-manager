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
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
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

      return `
        <div class="device-card">
          <div class="device-info">
            <span class="device-name">${escapeHtml(d.name)}</span>
            <span class="device-status ${statusClass}">${statusText}</span>
            <div class="device-address">${escapeHtml(d.address)}${rssiDisplay}</div>
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
      (s) => `
      <div class="sink-card">
        <div>
          <div class="sink-name">${escapeHtml(s.description || s.name)}</div>
          <div class="sink-description">${escapeHtml(s.name)}</div>
        </div>
        <div>
          <span class="device-status status-connected">${escapeHtml(s.state)}</span>
        </div>
      </div>
    `
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
    const result = await apiPost("/api/scan");
    renderDevices(result.devices);
    // Also refresh sinks
    const sinkResult = await apiGet("/api/audio/sinks");
    renderSinks(sinkResult.sinks);
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
    await refreshDevices();
  } catch (e) {
    showError(`Pairing failed: ${e.message}`);
    hideStatus();
  }
}

async function connectDevice(address) {
  try {
    showStatus(`Connecting to ${address}...`);
    await apiPost("/api/connect", { address });
    await refreshDevices();
  } catch (e) {
    showError(`Connection failed: ${e.message}`);
    hideStatus();
  }
}

async function disconnectDevice(address) {
  try {
    showStatus(`Disconnecting ${address}...`);
    await apiPost("/api/disconnect", { address });
    await refreshDevices();
  } catch (e) {
    showError(`Disconnect failed: ${e.message}`);
    hideStatus();
  }
}

async function forgetDevice(address) {
  if (!confirm(`Forget device ${address}? This will unpair it.`)) return;
  try {
    showStatus(`Removing ${address}...`);
    await apiPost("/api/forget", { address });
    await refreshDevices();
  } catch (e) {
    showError(`Forget failed: ${e.message}`);
    hideStatus();
  }
}

// -- Init --

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-scan").addEventListener("click", scanDevices);
  $("#btn-refresh").addEventListener("click", refreshDevices);

  // Initial load
  refreshDevices();
});
