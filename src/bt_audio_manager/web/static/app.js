/**
 * Bluetooth Audio Manager — Ingress UI
 *
 * Vanilla JS interface with Bootstrap 5.3 components.
 * Communicates with the app's REST API via WebSocket for real-time updates.
 */

// ============================================
// Section 1: Constants & Helpers
// ============================================

const API_BASE = document.location.pathname.replace(/\/$/, "");
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

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

async function apiPut(path, body = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => null);
    throw new Error(data?.error || `API error: ${resp.status}`);
  }
  return resp.json();
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text || "";
  return div.innerHTML;
}

function safeJsString(text) {
  // Escape for embedding in a JS single-quoted string inside an HTML attribute.
  // Order matters: backslashes first, then quotes, then HTML-significant chars.
  return (text || "")
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")
    .replace(/"/g, "\\x22")
    .replace(/</g, "\\x3c")
    .replace(/>/g, "\\x3e")
    .replace(/&/g, "\\x26");
}

// ============================================
// Section 2: Theme Detection
// ============================================

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  document.documentElement.setAttribute("data-bs-theme", e.matches ? "dark" : "light");
});

// ============================================
// Section 3: View Switching
// ============================================

function switchView(viewName) {
  // Hide all view panels
  $$(".view-panel").forEach((el) => el.classList.add("d-none"));
  // Show selected view
  const target = $(`#view-${viewName}`);
  if (target) target.classList.remove("d-none");
}

// ============================================
// Section 4: Toast Notifications
// ============================================

function showToast(message, level = "info") {
  const container = $("#toast-container");
  const icons = {
    info: "fas fa-info-circle text-primary",
    success: "fas fa-check-circle text-success",
    warning: "fas fa-exclamation-triangle text-warning",
    error: "fas fa-times-circle text-danger",
  };
  const titles = {
    info: "Info",
    success: "Success",
    warning: "Warning",
    error: "Error",
  };

  const toastEl = document.createElement("div");
  toastEl.className = "toast";
  toastEl.setAttribute("role", "alert");
  toastEl.innerHTML = `
    <div class="toast-header">
      <i class="${icons[level] || icons.info} me-2"></i>
      <strong class="me-auto">${titles[level] || "Notice"}</strong>
      <button type="button" class="btn-close" data-bs-dismiss="toast"></button>
    </div>
    <div class="toast-body">${escapeHtml(message)}</div>
  `;

  container.appendChild(toastEl);
  const toast = new bootstrap.Toast(toastEl, { delay: 5000 });
  toastEl.addEventListener("hidden.bs.toast", () => toastEl.remove());
  toast.show();
}

// ============================================
// Section 5: Operation Banner (contained alert)
// ============================================

function showBanner(text) {
  hideBanner(); // Remove any existing operation alert
  const container = $("#alert-container");
  if (!container) return;
  const el = document.createElement("div");
  el.id = "operation-alert";
  el.className = "alert alert-info d-flex align-items-center gap-2 mb-3";
  el.setAttribute("role", "alert");
  el.innerHTML = `
    <div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Loading...</span></div>
    <span>${escapeHtml(text)}</span>
  `;
  container.prepend(el);
}

function hideBanner() {
  const existing = $("#operation-alert");
  if (existing) existing.remove();
}

// ============================================
// Section 5a: Scanning State
// ============================================

let isScanning = false;
let scanTimerId = null;
let scanSecondsRemaining = 0;

function setScanningState(scanning, duration) {
  isScanning = scanning;
  if (scanning && duration) {
    scanSecondsRemaining = duration;
    clearInterval(scanTimerId);
    scanTimerId = setInterval(() => {
      scanSecondsRemaining--;
      if (scanSecondsRemaining <= 0) {
        clearInterval(scanTimerId);
        scanTimerId = null;
      }
      updateAddDeviceTile();
    }, 1000);
  } else {
    clearInterval(scanTimerId);
    scanTimerId = null;
    scanSecondsRemaining = 0;
  }
  updateAddDeviceTile();
}

function updateAddDeviceTile() {
  const tile = $("#add-device-tile");
  if (!tile) return;
  const body = tile.querySelector(".card-body");
  if (!body) return;
  if (isScanning) {
    tile.classList.add("scanning");
    const label = scanSecondsRemaining > 0
      ? `Scanning\u2026 ${scanSecondsRemaining}s`
      : "Finishing\u2026";
    body.innerHTML = `
      <i class="fas fa-spinner fa-spin"></i>
      <span>${label}</span>
    `;
  } else {
    tile.classList.remove("scanning");
    body.innerHTML = `
      <i class="fas fa-plus"></i>
      <span>Add Device</span>
    `;
  }
}

// ============================================
// Section 5b: Reconnect Banner & Connection Status
// ============================================

let reconnectTimerId = null;
let reconnectStartTime = null;

function showReconnectBanner() {
  if ($("#reconnect-alert")) return; // Already showing
  const container = $("#alert-container");
  if (!container) return;
  document.body.classList.add("server-unavailable");
  reconnectStartTime = Date.now();

  const el = document.createElement("div");
  el.id = "reconnect-alert";
  el.className = "alert alert-warning d-flex align-items-center gap-2 mb-3";
  el.setAttribute("role", "alert");
  el.innerHTML = `
    <div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Reconnecting...</span></div>
    <span>Reconnecting to server\u2026</span>
    <span id="reconnect-elapsed" class="text-muted small"></span>
  `;
  container.prepend(el);

  // Update elapsed time every second
  clearInterval(reconnectTimerId);
  reconnectTimerId = setInterval(() => {
    const elapsed = Math.floor((Date.now() - reconnectStartTime) / 1000);
    const mins = Math.floor(elapsed / 60);
    const secs = elapsed % 60;
    const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
    const elapsedEl = $("#reconnect-elapsed");
    if (elapsedEl) elapsedEl.textContent = `(${timeStr})`;
  }, 1000);
}

function hideReconnectBanner() {
  const existing = $("#reconnect-alert");
  if (existing) existing.remove();
  document.body.classList.remove("server-unavailable");
  clearInterval(reconnectTimerId);
  reconnectTimerId = null;
  reconnectStartTime = null;
}

// ============================================
// Section 6: BT Profile UUID Labels
// ============================================

const BT_PROFILES = {
  "0000110b-0000-1000-8000-00805f9b34fb": "A2DP Sink",
  "0000110a-0000-1000-8000-00805f9b34fb": "A2DP Source",
  "0000110c-0000-1000-8000-00805f9b34fb": "AVRCP Target",
  "0000110e-0000-1000-8000-00805f9b34fb": "AVRCP Controller",
  "0000111e-0000-1000-8000-00805f9b34fb": "HFP",
  "00001108-0000-1000-8000-00805f9b34fb": "HSP",
};

function profileLabels(uuids) {
  if (!uuids || uuids.length === 0) return "";
  const labels = uuids
    .map((u) => BT_PROFILES[u.toLowerCase()])
    .filter(Boolean);
  return labels.length > 0 ? "Supports: " + labels.join(" \u00b7 ") : "";
}

function buildCapBadges(device) {
  if (!device.connected) return "";
  const badges = [];
  // Bearer type (BR/EDR, LE)
  if (device.bearers) {
    for (const b of device.bearers) {
      badges.push(`<span class="cap-badge bg-secondary" title="${b === "BR/EDR" ? "Classic Bluetooth" : "Bluetooth Low Energy"}">${escapeHtml(b)}</span>`);
    }
  }
  // Audio profile badges — show selected profile with checkmark
  const uuids = (device.uuids || []).map((u) => u.toLowerCase());
  const activeProfile = device.audio_profile || "a2dp";
  const hasA2dpSink = uuids.some((u) => u.startsWith("0000110b"));
  const hasHfpHsp = uuids.some((u) => u.startsWith("0000111e") || u.startsWith("00001108"));
  if (hasA2dpSink) {
    if (window._hfpSwitchingEnabled && activeProfile !== "a2dp") {
      badges.push('<span class="cap-badge bg-info" title="A2DP stereo audio available">A2DP</span>');
    } else {
      badges.push('<span class="cap-badge bg-success" title="A2DP stereo audio (active)">A2DP \u2713</span>');
    }
  }
  if (window._hfpSwitchingEnabled && hasHfpHsp) {
    if (activeProfile === "hfp") {
      badges.push('<span class="cap-badge bg-success" title="HFP/HSP mono + mic (active)">HFP \u2713</span>');
    } else {
      badges.push('<span class="cap-badge bg-info" title="Hands-Free / Headset Profile available">HFP</span>');
    }
  }
  // AVRCP
  const hasAvrcp = uuids.some((u) => u.startsWith("0000110c") || u.startsWith("0000110e"));
  if (hasAvrcp) {
    if (device.avrcp_enabled !== false) {
      badges.push('<span class="cap-badge bg-success" title="Media buttons enabled">AVRCP \u2713</span>');
    } else {
      badges.push('<span class="cap-badge bg-warning text-dark" title="Media buttons disabled">AVRCP \u2717</span>');
    }
  }
  return badges.length > 0
    ? `<div class="d-flex flex-wrap gap-1 mb-1">${badges.join("")}</div>`
    : "";
}

function buildFeatureBadges(device) {
  const badges = [];
  const im = device.idle_mode || "default";
  if (im === "power_save") {
    badges.push('<span class="feature-badge border-info text-info"><i class="fas fa-moon me-1"></i>Power Save</span>');
  } else if (im === "keep_alive" && device.keep_alive_active) {
    badges.push('<span class="feature-badge border-danger text-danger"><i class="fas fa-heartbeat me-1"></i>Stay Awake</span>');
  } else if (im === "auto_disconnect") {
    badges.push('<span class="feature-badge border-warning text-warning"><i class="fas fa-plug me-1"></i>Auto-Disconnect</span>');
  }
  if (device.mpd_enabled) {
    badges.push(`<span class="feature-badge border-primary text-primary"><i class="fas fa-music me-1"></i>MPD :${device.mpd_port || "?"}</span>`);
  }
  return badges.join("");
}

// ============================================
// Section 7: Device Rendering (Responsive Grid)
// ============================================

// Cached sinks for merging into device cards
let currentSinks = [];

function renderAddDeviceTile() {
  const scanLabel = isScanning
    ? (scanSecondsRemaining > 0 ? `Scanning\u2026 ${scanSecondsRemaining}s` : "Finishing\u2026")
    : "Add Device";
  const scanIcon = isScanning
    ? '<i class="fas fa-spinner fa-spin"></i>'
    : '<i class="fas fa-plus"></i>';
  const scanClass = isScanning ? " scanning" : "";
  const wrapper = $("#add-device-wrapper");
  if (wrapper) {
    wrapper.innerHTML = `
      <div class="card add-device-tile${scanClass}" id="add-device-tile"
           onclick="scanDevices()" role="button" tabindex="0"
           title="Scan for nearby Bluetooth audio devices">
        <div class="card-body">
          ${scanIcon}
          <span>${scanLabel}</span>
        </div>
      </div>
    `;
  }
}

function renderDevices(devices) {
  const grid = $("#devices-grid");
  renderAddDeviceTile();

  if (!devices || devices.length === 0) {
    grid.innerHTML = "";
    return;
  }

  grid.innerHTML = devices
    .map((d) => {
      const badgeClass = d.connected
        ? "badge-connected"
        : d.paired
          ? "badge-paired"
          : "badge-discovered";
      const statusText = d.connected
        ? "Connected"
        : d.paired
          ? "Paired"
          : "Discovered";

      // Action buttons (primary actions only — Forget is in kebab menu)
      let actions = "";
      if (d.connected) {
        actions = `
          <button type="button" class="btn btn-sm btn-outline-danger" onclick="disconnectDevice('${d.address}')">
            <i class="fas fa-unlink me-1"></i>Disconnect
          </button>
        `;
      } else if (d.paired || d.stored) {
        actions = `
          <button type="button" class="btn btn-sm btn-success" onclick="connectDevice('${d.address}')">
            <i class="fas fa-link me-1"></i>Connect
          </button>
        `;
      } else {
        actions = `
          <button type="button" class="btn btn-sm btn-primary" onclick="pairDevice('${d.address}')">
            <i class="fas fa-handshake me-1"></i>Pair
          </button>
          <button type="button" class="btn btn-sm btn-outline-secondary" onclick="dismissDevice('${d.address}')" title="Dismiss">
            <i class="fas fa-times"></i>
          </button>
        `;
      }

      // Kebab dropdown for paired/stored devices (Settings + Forget)
      const idleMode = (d.stored || d.paired) ? (d.idle_mode || "default") : "default";
      let kebab = "";
      if (d.stored || d.paired) {
        const audioProfile = d.audio_profile || "a2dp";
        const kaMethod = d.keep_alive_method || "infrasound";
        const powerSaveDelay = d.power_save_delay ?? 0;
        const autoDisconnectMinutes = d.auto_disconnect_minutes ?? 30;
        const mpdEnabled = d.mpd_enabled || false;
        const mpdPort = d.mpd_port || "";
        const mpdHwVolume = d.mpd_hw_volume ?? 100;
        const avrcpEnabled = d.avrcp_enabled ?? true;
        const safeName = safeJsString(d.name);
        const uuidsJson = safeJsString(JSON.stringify(d.uuids || []));
        kebab = `
          <div class="dropdown">
            <button class="btn btn-sm btn-link text-muted p-0 ms-2" type="button"
                    data-bs-toggle="dropdown" title="Device options">
              <i class="fas fa-ellipsis-v"></i>
            </button>
            <ul class="dropdown-menu dropdown-menu-end">
              <li><a class="dropdown-item" href="#" onclick="openDeviceSettings('${d.address}', '${safeName}', '${audioProfile}', '${idleMode}', '${kaMethod}', ${powerSaveDelay}, ${autoDisconnectMinutes}, ${mpdEnabled}, '${mpdPort}', ${mpdHwVolume}, ${avrcpEnabled}, '${uuidsJson}'); return false;">
                <i class="fas fa-cog me-2"></i>Settings
              </a></li>
              ${d.connected ? `<li><a class="dropdown-item" href="#" onclick="forceReconnectDevice('${d.address}'); return false;">
                <i class="fas fa-sync me-2"></i>Force Reconnect
              </a></li>` : ""}
              <li><hr class="dropdown-divider"></li>
              <li><a class="dropdown-item text-danger" href="#" onclick="forgetDevice('${d.address}'); return false;">
                <i class="fas fa-trash me-2"></i>Forget Device
              </a></li>
            </ul>
          </div>
        `;
      }

      const rssiDisplay = d.rssi ? ` (${d.rssi} dBm)` : "";
      const profiles = profileLabels(d.uuids);

      // Merge sink info for connected devices
      let sinkInfo = "";
      if (d.connected) {
        const macNorm = d.address.replace(/:/g, "_").toLowerCase();
        const matchedSink = currentSinks.find(
          (s) => s.name && s.name.toLowerCase().includes(macNorm),
        );
        if (matchedSink) {
          const audioParts = [
            matchedSink.sample_rate ? `${(matchedSink.sample_rate / 1000).toFixed(1)} kHz` : null,
            matchedSink.channels ? `${matchedSink.channels}ch` : null,
            matchedSink.format || null,
          ].filter(Boolean);
          const vol = matchedSink.mute ? "Muted" : `${matchedSink.volume}%`;
          const stateMap = { running: "Streaming", idle: "Idle", suspended: "Suspended" };
          const stateLabel = stateMap[matchedSink.state] || matchedSink.state;
          sinkInfo = `
            <div class="mt-2 small text-muted">
              <i class="fas fa-music me-1"></i>${audioParts.length ? escapeHtml(audioParts.join(" / ")) + " &middot; " : ""}${escapeHtml(vol)}
              <span class="badge bg-secondary ms-1">${escapeHtml(stateLabel)}</span>
            </div>
          `;
        }
      }

      return `
        <div class="col-md-6 col-lg-4">
          <div class="card device-card h-100">
            <div class="card-body">
              <div class="d-flex justify-content-between align-items-start mb-2">
                <h5 class="card-title mb-0" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</h5>
                <div class="d-flex align-items-center gap-1">
                  <span class="badge ${badgeClass}">${statusText}</span>
                  ${kebab}
                </div>
              </div>
              ${buildCapBadges(d)}
              <div class="device-meta-text font-monospace text-muted">${escapeHtml(d.address)}${rssiDisplay}${d.adapter ? ` on ${escapeHtml(d.adapter)}` : ""}</div>
              ${profiles ? `<div class="device-meta-text device-profiles-text mt-1 text-muted">${escapeHtml(profiles)}</div>` : ""}
              ${sinkInfo}
              ${(() => { const fb = buildFeatureBadges(d); return fb ? `<div class="device-feature-badges d-flex gap-2 flex-wrap">${fb}</div>` : ""; })()}
              <div class="device-actions">
                ${actions}
              </div>
            </div>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderSinks(sinks) {
  // Store sinks for device card merging; re-render devices to update sink info
  currentSinks = sinks || [];
}

// ============================================
// Section 8: Adapter Modal Rendering
// ============================================

function renderAdaptersModal(adapters) {
  const container = $("#adapters-container");

  if (!adapters || adapters.length === 0) {
    container.innerHTML = '<p class="text-center text-muted py-3">No Bluetooth adapters found.</p>';
    return;
  }

  container.innerHTML = adapters
    .map((a) => {
      const selectedBadge = a.selected
        ? '<span class="badge bg-success ms-2">In Use</span>'
        : "";
      const bleScanBadge = a.ble_scanning
        ? '<span class="badge bg-warning ms-2">HA BLE Scanning</span>'
        : "";
      const haManagedBadge = a.ha_managed
        ? '<span class="badge bg-info ms-2">HA Bluetooth</span>'
        : "";
      const poweredBadge = a.powered
        ? '<span class="badge bg-success">Powered</span>'
        : '<span class="badge bg-secondary">Off</span>';

      // Friendly name: prefer resolved hw_model (not raw modalias), else alias
      // Filter out hostname-like aliases (contain dots) — BlueZ defaults alias to hostname
      const hwResolved = a.hw_model && a.hw_model !== a.modalias;
      const aliasUseful = a.alias && a.alias !== a.name && !a.alias.includes(".");
      const friendlyName = hwResolved ? a.hw_model : (aliasUseful ? a.alias : "");

      // Technical line: hci name + modalias
      const techParts = [a.name];
      if (a.modalias) techParts.push(a.modalias);
      const techLine = techParts.join(" \u2014 ");

      const displayLabel = friendlyName || a.name;
      const selectBtn =
        !a.selected && a.powered
          ? `<button type="button" class="btn btn-sm btn-primary" onclick="selectAdapter('${a.address}', '${safeJsString(displayLabel)}')">
               <i class="fas fa-check me-1"></i>Select
             </button>`
          : "";

      return `
        <div class="card adapter-card mb-2">
          <div class="card-body d-flex justify-content-between align-items-center py-2">
            <div>
              ${friendlyName ? `<div class="fw-semibold">${escapeHtml(friendlyName)}</div>` : ""}
              <div class="${friendlyName ? "small text-muted" : "fw-semibold"}">${escapeHtml(techLine)}</div>
              <div class="font-monospace small text-muted">${escapeHtml(a.address)}</div>
            </div>
            <div class="d-flex align-items-center gap-2">
              ${poweredBadge}${selectedBadge}${haManagedBadge}${bleScanBadge}
              ${selectBtn}
            </div>
          </div>
        </div>
      `;
    })
    .join("");
}

function openAdaptersModal() {
  const modal = new bootstrap.Modal("#adaptersModal");
  modal.show();
  loadAdapters();
}

async function loadAdapters() {
  try {
    const data = await apiGet("/api/adapters");
    renderAdaptersModal(data.adapters);
  } catch (e) {
    console.warn("Failed to load adapters:", e.message);
  }
}

// ============================================
// Section 9: Events Log (Combined MPRIS + AVRCP)
// ============================================

const MAX_EVENT_ENTRIES = 100;
let eventCount = 0;

function appendEventEntry(type, html) {
  const log = $("#events-log");

  // Remove placeholder if present
  const placeholder = log.querySelector(".text-center");
  if (placeholder) placeholder.remove();

  const entry = document.createElement("div");
  entry.className = "event-entry";
  entry.innerHTML = html;
  log.appendChild(entry);

  // Trim old entries
  while (log.children.length > MAX_EVENT_ENTRIES) {
    log.removeChild(log.firstChild);
  }

  eventCount = log.children.length;
  $("#events-count").textContent = eventCount;

  // Auto-scroll
  log.scrollTop = log.scrollHeight;
}

function eventTime(data) {
  const d = data.ts ? new Date(data.ts * 1000) : new Date();
  return d.toLocaleTimeString();
}

function deviceNameByAddress(address) {
  if (!address || !lastDevices) return "";
  const dev = lastDevices.find((d) => d.address === address);
  return dev ? dev.name : "";
}

function deviceNameTag(address) {
  const name = deviceNameByAddress(address);
  if (!name) return "";
  return ` <span class="text-muted">[${escapeHtml(name)}]</span>`;
}

function appendMprisCommand(data) {
  // Use resolved address from backend if available, else infer from single connected device
  let nameHtml = "";
  if (data.address) {
    nameHtml = deviceNameTag(data.address);
  } else if (lastDevices) {
    const connected = lastDevices.filter((d) => d.connected);
    if (connected.length === 1) {
      nameHtml = ` <span class="text-muted">[${escapeHtml(connected[0].name)}]</span>`;
    }
  }
  appendEventEntry(
    "mpris",
    `<span class="event-time">${escapeHtml(eventTime(data))}</span>`
    + `<span class="event-type mpris">MPRIS</span>`
    + `<span class="event-content"><strong>${escapeHtml(data.command)}</strong>`
    + (data.detail ? ` <span class="text-muted">${escapeHtml(data.detail)}</span>` : "")
    + nameHtml
    + `</span>`,
  );
}

// Volume event deduplication — suppress duplicates within 1.5s window
const _lastVolumeEvent = {};  // address → {value, ts}
const VOLUME_DEDUP_MS = 1500;

function _deviceHasAvrcp(address) {
  if (!address || !lastDevices) return false;
  const dev = lastDevices.find((d) => d.address === address);
  if (!dev || !dev.uuids) return false;
  return dev.uuids.some((u) => u.startsWith("0000110c") || u.startsWith("0000110e"));
}

function appendAvrcpEvent(data) {
  // Deduplicate volume events (D-Bus, PulseAudio, and AVRCP can all fire)
  if (data.property === "Volume" && data.address) {
    const now = Date.now();
    const prev = _lastVolumeEvent[data.address];
    if (prev && prev.value === String(data.value) && (now - prev.ts) < VOLUME_DEDUP_MS) {
      return; // suppress duplicate
    }
    _lastVolumeEvent[data.address] = { value: String(data.value), ts: now };
  }

  const valueStr = typeof data.value === "object"
    ? JSON.stringify(data.value)
    : String(data.value);

  // Label as "Transport" for devices without AVRCP UUIDs (e.g. initial A2DP volume)
  const isAvrcp = _deviceHasAvrcp(data.address);
  const label = isAvrcp ? "AVRCP" : "Transport";
  const cssClass = isAvrcp ? "avrcp" : "transport";

  appendEventEntry(
    cssClass,
    `<span class="event-time">${escapeHtml(eventTime(data))}</span>`
    + `<span class="event-type ${cssClass}">${label}</span>`
    + `<span class="event-content"><strong>${escapeHtml(data.property)}</strong> = `
    + `<span class="text-success">${escapeHtml(valueStr)}</span>`
    + deviceNameTag(data.address)
    + `</span>`,
  );
}

function clearEvents() {
  const log = $("#events-log");
  log.innerHTML = `
    <div class="text-center py-4 text-muted">
      <i class="fas fa-satellite-dish fa-2x mb-2 d-block opacity-50"></i>
      No events yet. Connect a device and press buttons on it.
    </div>
  `;
  eventCount = 0;
  $("#events-count").textContent = "0";
}

// ============================================
// Section 10: App Log Viewer
// ============================================

const MAX_LOG_ENTRIES = 1000;
let allLogEntries = [];
let logSearchTimer = null;

function appendLogEntry(data) {
  allLogEntries.push(data);

  // Trim buffer
  if (allLogEntries.length > MAX_LOG_ENTRIES) {
    allLogEntries = allLogEntries.slice(-MAX_LOG_ENTRIES);
  }

  // If live is off, don't render
  const liveToggle = $("#log-live");
  if (liveToggle && !liveToggle.checked) return;

  // If filters are active, check before rendering
  if (!matchesLogFilter(data)) return;

  renderSingleLogEntry(data, true);
  updateLogsCount();
}

function matchesLogFilter(entry) {
  const levelFilter = $("#log-level-filter").value;
  if (levelFilter && entry.level !== levelFilter) return false;

  const searchInput = $("#log-search-input").value.toLowerCase();
  if (searchInput && !entry.message.toLowerCase().includes(searchInput)
      && !(entry.logger || "").toLowerCase().includes(searchInput)) {
    return false;
  }

  return true;
}

function renderSingleLogEntry(entry, isNew) {
  const container = $("#logs-container");

  // Remove placeholder if present
  const placeholder = container.querySelector(".text-center");
  if (placeholder) placeholder.remove();

  const el = document.createElement("div");
  el.className = `log-entry${isNew ? " new" : ""}`;

  const d = new Date(entry.ts * 1000);
  const ts = d.toLocaleTimeString() + "." + String(d.getMilliseconds()).padStart(3, "0");
  const levelClass = entry.level.toLowerCase();
  const logger = (entry.logger || "").split(".").pop();

  el.innerHTML =
    `<span class="log-timestamp">${escapeHtml(ts)}</span>`
    + `<span class="log-level ${levelClass}">${escapeHtml(entry.level)}</span>`
    + `<span class="log-logger">${escapeHtml(logger)}</span>`
    + `<span class="log-message">${escapeHtml(entry.message)}</span>`;

  container.appendChild(el);

  // Trim displayed entries
  while (container.children.length > MAX_LOG_ENTRIES) {
    container.removeChild(container.firstChild);
  }

  // Auto-scroll
  const autoScroll = $("#log-auto-scroll");
  if (autoScroll && autoScroll.checked) {
    container.scrollTop = container.scrollHeight;
  }
}

function filterLogs() {
  renderAllFilteredLogs();
}

function debouncedLogSearch() {
  clearTimeout(logSearchTimer);
  logSearchTimer = setTimeout(renderAllFilteredLogs, 300);
}

function renderAllFilteredLogs() {
  const container = $("#logs-container");
  container.innerHTML = "";

  const filtered = allLogEntries.filter(matchesLogFilter);
  filtered.forEach((entry) => renderSingleLogEntry(entry, false));

  if (filtered.length === 0) {
    container.innerHTML = `
      <div class="text-center py-5 text-muted">
        <p>No matching log entries.</p>
      </div>
    `;
  }

  updateLogsCount();

  // Scroll to bottom
  const autoScroll = $("#log-auto-scroll");
  if (autoScroll && autoScroll.checked) {
    container.scrollTop = container.scrollHeight;
  }
}

function updateLogsCount() {
  const count = $("#logs-container").querySelectorAll(".log-entry").length;
  $("#logs-count").textContent = count;
}

// ============================================
// Section 11: Actions
// ============================================

async function scanDevices() {
  if (isScanning) return;
  try {
    const result = await apiPost("/api/scan");
    if (result.scanning) {
      setScanningState(true, result.duration);
    }
  } catch (e) {
    showToast(`Scan failed: ${e.message}`, "error");
  }
}

async function pairDevice(address) {
  try {
    await apiPost("/api/pair", { address });
  } catch (e) {
    showToast(`Pairing failed: ${e.message}`, "error");
  }
}

async function connectDevice(address) {
  try {
    await apiPost("/api/connect", { address });
  } catch (e) {
    showToast(`Connection failed: ${e.message}`, "error");
  }
}

async function disconnectDevice(address) {
  try {
    await apiPost("/api/disconnect", { address });
  } catch (e) {
    showToast(`Disconnect failed: ${e.message}`, "error");
  }
}

async function forceReconnectDevice(address) {
  try {
    await apiPost("/api/force-reconnect", { address });
  } catch (e) {
    showToast(`Force reconnect failed: ${e.message}`, "error");
  }
}

async function dismissDevice(address) {
  try {
    await apiPost("/api/forget", { address });
  } catch (e) {
    showToast(`Dismiss failed: ${e.message}`, "error");
  }
}

let _pendingForgetAddress = null;

function forgetDevice(address) {
  _pendingForgetAddress = address;
  $("#forget-device-address").textContent = address;
  new bootstrap.Modal("#forgetDeviceModal").show();
}

async function doForgetDevice() {
  if (!_pendingForgetAddress) return;
  const address = _pendingForgetAddress;
  _pendingForgetAddress = null;
  bootstrap.Modal.getInstance($("#forgetDeviceModal"))?.hide();
  try {
    await apiPost("/api/forget", { address });
  } catch (e) {
    showToast(`Forget failed: ${e.message}`, "error");
  }
}

let _pendingAdapterMac = null;
let _pendingAdapterLabel = null;

async function selectAdapter(adapterMac, displayLabel) {
  // If no devices are stored/paired, skip the warning — nothing to lose
  const hasDevices = lastDevices && lastDevices.some((d) => d.stored || d.paired);
  if (!hasDevices) {
    await doAdapterSwitch(adapterMac, displayLabel, false);
    return;
  }

  // Show confirmation modal with pairing-loss warning
  _pendingAdapterMac = adapterMac;
  _pendingAdapterLabel = displayLabel;
  $("#switch-adapter-name").textContent = displayLabel;
  new bootstrap.Modal("#adapterSwitchModal").show();
}

async function doAdapterSwitch(adapterMac, displayLabel, clean) {
  try {
    // Close both modals — they will have stale data until the server returns
    bootstrap.Modal.getInstance($("#adapterSwitchModal"))?.hide();
    bootstrap.Modal.getInstance($("#adaptersModal"))?.hide();
    showBanner(
      clean
        ? `Cleaning devices and switching to ${displayLabel}...`
        : `Switching to adapter ${displayLabel}...`
    );

    // Backend handles disconnect-all + forget-all when clean=true,
    // and pushes live progress via WebSocket status messages.
    // adapter value is now the MAC address (stable across reboots).
    const result = await apiPost("/api/set-adapter", {
      adapter: adapterMac,
      clean: clean,
    });
    if (result.restart_required) {
      showBanner("Restarting app with new adapter...");
      // Fire-and-forget: the server will die during restart, so the
      // response will never arrive (expected 502). The WebSocket
      // reconnect loop will detect when the server is back.
      apiPost("/api/restart").catch(() => {});
    }
  } catch (e) {
    hideBanner();
    showToast(`Adapter switch failed: ${e.message}`, "error");
  }
}

// ============================================
// Section 11b: Add-on Settings Modal
// ============================================

async function openSettingsModal() {
  try {
    const data = await apiGet("/api/settings");
    $("#setting-auto-reconnect").checked = data.auto_reconnect;
    $("#setting-reconnect-interval").value = data.reconnect_interval_seconds;
    $("#setting-reconnect-max-backoff").value = data.reconnect_max_backoff_seconds;
    $("#setting-scan-duration").value = data.scan_duration_seconds;
    new bootstrap.Modal("#settingsModal").show();
  } catch (e) {
    showToast(`Failed to load settings: ${e.message}`, "error");
  }
}

async function saveSettings() {
  const settings = {
    auto_reconnect: $("#setting-auto-reconnect").checked,
    reconnect_interval_seconds: parseInt($("#setting-reconnect-interval").value, 10),
    reconnect_max_backoff_seconds: parseInt($("#setting-reconnect-max-backoff").value, 10),
    scan_duration_seconds: parseInt($("#setting-scan-duration").value, 10),
  };
  try {
    await apiPut("/api/settings", settings);
    showToast("Settings saved", "success");
    bootstrap.Modal.getInstance($("#settingsModal"))?.hide();
  } catch (e) {
    showToast(`Failed to save settings: ${e.message}`, "error");
  }
}

// ============================================
// Section 11c: Device Settings Modal
// ============================================

let _settingsAddress = null;

function openDeviceSettings(address, name, audioProfile, idleMode, kaMethod, powerSaveDelay, autoDisconnectMinutes, mpdEnabled, mpdPort, mpdHwVolume, avrcpEnabled, uuidsJson) {
  _settingsAddress = address;
  $("#device-settings-name").textContent = name;
  $("#device-settings-address").textContent = address;

  // Parse UUIDs once — used by both Audio Profile and AVRCP sections
  const uuids = typeof uuidsJson === "string" ? JSON.parse(uuidsJson) : (uuidsJson || []);
  const lowerUuids = uuids.map(u => u.toLowerCase());

  // Audio Profile — hidden when HFP switching is disabled (SCO unavailable)
  const profileSection = $("#setting-audio-profile").closest(".mb-3");
  if (window._hfpSwitchingEnabled) {
    profileSection.style.display = "";
    const HFP_UUID = "0000111e-0000-1000-8000-00805f9b34fb";
    const HSP_UUID = "00001108-0000-1000-8000-00805f9b34fb";
    const hasHfp = lowerUuids.includes(HFP_UUID) || lowerUuids.includes(HSP_UUID);
    const profileSelect = $("#setting-audio-profile");
    profileSelect.value = audioProfile || "a2dp";
    const hfpOption = profileSelect.querySelector('option[value="hfp"]');
    if (hfpOption) hfpOption.disabled = !hasHfp;
    const profileHelp = $("#audio-profile-help");
    if ((audioProfile || "a2dp") === "hfp") {
      profileHelp.textContent = "Mono audio with microphone input. Use with Wyoming Satellite for voice assistant.";
    } else {
      profileHelp.textContent = "Stereo high-quality audio for music and media playback.";
    }
    profileSelect.onchange = () => {
      const v = profileSelect.value;
      $("#audio-profile-help").textContent = v === "hfp"
        ? "Mono audio with microphone input. Use with Wyoming Satellite for voice assistant."
        : "Stereo high-quality audio for music and media playback.";
    };
  } else {
    profileSection.style.display = "none";
  }

  $("#setting-idle-mode").value = idleMode || "default";
  $("#setting-keep-alive-method").value = kaMethod || "infrasound";
  $("#setting-power-save-delay").value = String(powerSaveDelay ?? 0);
  $("#setting-auto-disconnect-minutes").value = String(autoDisconnectMinutes ?? 30);
  $("#setting-mpd-enabled").checked = mpdEnabled || false;
  $("#setting-mpd-hw-volume").value = mpdHwVolume ?? 100;
  $("#setting-mpd-port").value = mpdPort || "";
  // Show connection info if port is assigned
  if (mpdPort) {
    $("#mpd-port-display").textContent = mpdPort;
    $("#mpd-hostname").textContent = location.hostname;
    $("#mpd-connection-info").style.display = "";
  } else {
    $("#mpd-connection-info").style.display = "none";
  }
  // AVRCP toggle — disable if device lacks AVRCP UUIDs
  const AVRCP_TARGET = "0000110c-0000-1000-8000-00805f9b34fb";
  const AVRCP_CONTROLLER = "0000110e-0000-1000-8000-00805f9b34fb";
  const hasAvrcp = lowerUuids.includes(AVRCP_TARGET) || lowerUuids.includes(AVRCP_CONTROLLER);
  const avrcpToggle = $("#setting-avrcp-enabled");
  const avrcpHelp = $("#avrcp-help-text");
  avrcpToggle.checked = hasAvrcp ? (avrcpEnabled ?? true) : false;
  avrcpToggle.disabled = !hasAvrcp;
  if (hasAvrcp) {
    avrcpHelp.textContent = "Track playback state and accept media-button commands from the speaker. Media buttons may or may not work reliably depending on hardware.";
  } else {
    avrcpHelp.textContent = "Device does not support AVRCP media buttons.";
  }
  toggleIdleModeOptions();
  toggleMpdConfigVisibility();
  new bootstrap.Modal("#deviceSettingsModal").show();
}

function toggleIdleModeOptions() {
  const mode = $("#setting-idle-mode").value;
  $("#power-save-options").style.display = mode === "power_save" ? "" : "none";
  $("#keep-alive-options").style.display = mode === "keep_alive" ? "" : "none";
  $("#auto-disconnect-options").style.display = mode === "auto_disconnect" ? "" : "none";
  const helpTexts = {
    default: "No action taken when audio stops. Whether the speaker sleeps depends on its own hardware idle timer.",
    power_save: "Suspends the audio sink after the delay to release the A2DP transport. The speaker's own internal sleep timer determines when it actually powers down.",
    keep_alive: "Streams inaudible audio to prevent the speaker from auto-shutting down during silence.",
    auto_disconnect: "Fully disconnects the Bluetooth device after the specified idle timeout.",
  };
  $("#idle-mode-help").textContent = helpTexts[mode] || "";
}

function toggleMpdConfigVisibility() {
  const enabled = $("#setting-mpd-enabled").checked;
  $("#mpd-config-group").style.display = enabled ? "" : "none";
  // Pre-fill port with next available when enabling MPD for the first time
  if (enabled && !$("#setting-mpd-port").value && lastDevices) {
    const usedPorts = new Set(
      lastDevices
        .filter((d) => d.mpd_port != null && d.address !== _settingsAddress)
        .map((d) => d.mpd_port)
    );
    for (let p = 6600; p <= 6609; p++) {
      if (!usedPorts.has(p)) {
        $("#setting-mpd-port").value = p;
        $("#mpd-port-display").textContent = p;
        $("#mpd-hostname").textContent = location.hostname;
        $("#mpd-connection-info").style.display = "";
        break;
      }
    }
  }
}

async function saveDeviceSettings() {
  if (!_settingsAddress) return;
  const btn = $("#btnSaveSettings");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';
  const idleMode = $("#setting-idle-mode").value;
  const settings = {
    idle_mode: idleMode,
    keep_alive_method: $("#setting-keep-alive-method").value,
    power_save_delay: parseInt($("#setting-power-save-delay").value, 10) || 0,
    auto_disconnect_minutes: parseInt($("#setting-auto-disconnect-minutes").value, 10) || 30,
    mpd_enabled: $("#setting-mpd-enabled").checked,
  };
  // Include MPD config when enabled
  if (settings.mpd_enabled) {
    settings.mpd_hw_volume = parseInt($("#setting-mpd-hw-volume").value, 10) || 100;
    const portVal = $("#setting-mpd-port").value;
    if (portVal) settings.mpd_port = parseInt(portVal, 10);
  }
  // Include audio profile only when HFP switching is enabled
  if (window._hfpSwitchingEnabled) {
    settings.audio_profile = $("#setting-audio-profile").value;
  }
  // Include AVRCP setting only if toggle is not disabled (device supports AVRCP)
  const avrcpToggle = $("#setting-avrcp-enabled");
  if (!avrcpToggle.disabled) {
    settings.avrcp_enabled = avrcpToggle.checked;
  }
  try {
    const resp = await apiPut(`/api/devices/${encodeURIComponent(_settingsAddress)}/settings`, settings);
    const port = resp.settings?.mpd_port;
    if (settings.mpd_enabled && port) {
      showToast(`Settings saved — MPD on port ${port}`, "success");
    } else {
      showToast("Device settings saved", "success");
    }
    bootstrap.Modal.getInstance($("#deviceSettingsModal"))?.hide();
  } catch (e) {
    showToast(`Failed to save settings: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="fas fa-save me-1"></i>Save';
  }
}

// ============================================
// Section 12: WebSocket (Real-time Updates)
// ============================================

let ws = null;
let wsReconnectDelay = 1000;
let _wsConnected = false;
const WS_MAX_DELAY = 30000;
const WS_BACKOFF = 1.5;

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${proto}//${location.host}${API_BASE}/api/ws`;
  console.log("[WS] Connecting to", wsUrl);

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log("[WS] Open (waiting for first message)");
  };

  ws.onmessage = (e) => {
    // Mark connected on first real message (not just TCP open)
    if (wsReconnectDelay !== 1000 || !_wsConnected) {
      _wsConnected = true;
      hideReconnectBanner();
      hideBanner(); // Clear any pending operation banner (e.g. adapter restart)
    }
    wsReconnectDelay = 1000; // Reset backoff on successful data
    const msg = JSON.parse(e.data);
    switch (msg.type) {
      case "devices_changed":
        renderDevices(msg.devices);
        break;
      case "sinks_changed":
        renderSinks(msg.sinks);
        // Re-render devices to update merged sink info
        refreshDevicesFromCache();
        break;
      case "settings_changed":
        // Settings updated by another client — no action needed unless modal is open
        break;
      case "mpris_command":
        appendMprisCommand(msg);
        break;
      case "avrcp_event":
        appendAvrcpEvent(msg);
        break;
      case "log_entry":
        appendLogEntry(msg);
        break;
      case "settings_changed":
        // Runtime settings updated by another client; no action needed
        break;
      case "keepalive_changed":
        // Devices list will be re-sent via devices_changed; toast for feedback
        if (msg.enabled) {
          showToast(`Keep-alive started for ${msg.address}`, "info");
        } else {
          showToast(`Keep-alive stopped for ${msg.address}`, "info");
        }
        break;
      case "scan_started":
        setScanningState(true, msg.duration);
        break;
      case "scan_finished":
        setScanningState(false);
        if (msg.error) {
          showToast(`Scan failed: ${msg.error}`, "error");
        }
        break;
      case "scan_state":
        // Sent on WS connect — sync scanning state
        if (msg.scanning && !isScanning) {
          setScanningState(true);
        } else if (!msg.scanning && isScanning) {
          setScanningState(false);
        }
        break;
      case "status":
        if (msg.message) {
          showBanner(msg.message);
        } else {
          hideBanner();
        }
        break;
      case "toast":
        showToast(msg.message, msg.level || "info");
        break;
      default:
        console.log("[WS] Unknown message type:", msg.type);
    }
  };

  ws.onclose = () => {
    console.log("[WS] Closed, reconnecting in", wsReconnectDelay, "ms");
    ws = null;
    _wsConnected = false;
    showReconnectBanner();
    setTimeout(connectWebSocket, wsReconnectDelay);
    wsReconnectDelay = Math.min(wsReconnectDelay * WS_BACKOFF, WS_MAX_DELAY);
  };

  ws.onerror = () => {
    console.warn("[WS] Error");
    ws.close();
  };
}

// Cache last known devices for re-rendering when sinks change
let lastDevices = null;

function refreshDevicesFromCache() {
  if (lastDevices) {
    renderDevices(lastDevices);
  }
}

// Wrap renderDevices to cache
const _origRenderDevices = renderDevices;
// We need to intercept — override via reassignment pattern
(function () {
  const grid = null; // Will be resolved at call time
  const origFn = renderDevices;

  window.renderDevices = function (devices) {
    lastDevices = devices;
    origFn(devices);
  };
})();

// ============================================
// Section 13: Init
// ============================================

document.addEventListener("DOMContentLoaded", () => {
  // Wire up idle mode dropdown in device settings modal
  const idleModeSelect = $("#setting-idle-mode");
  if (idleModeSelect) idleModeSelect.addEventListener("change", toggleIdleModeOptions);

  // Wire up forget-device confirmation button
  const confirmForgetBtn = $("#btn-confirm-forget");
  if (confirmForgetBtn) {
    confirmForgetBtn.addEventListener("click", () => doForgetDevice());
  }

  // Wire up adapter-switch confirmation button
  const confirmSwitchBtn = $("#btn-confirm-adapter-switch");
  if (confirmSwitchBtn) {
    confirmSwitchBtn.addEventListener("click", async () => {
      if (!_pendingAdapterMac) return;
      const mac = _pendingAdapterMac;
      const label = _pendingAdapterLabel;
      _pendingAdapterMac = null;
      _pendingAdapterLabel = null;
      await doAdapterSwitch(mac, label, true);
    });
  }

  // WebSocket provides real-time updates (initial state sent on connect)
  connectWebSocket();

  // Load adapter info (once at startup)
  loadAdapters();

  // Show version in header pill and footer; store feature flags
  apiGet("/api/info")
    .then((data) => {
      const ver = data.version;
      $("#build-version").textContent = ver;
      $("#version-label").textContent = `${ver} (${data.adapter})`;
      window._hfpSwitchingEnabled = !!data.hfp_switching_enabled;
    })
    .catch(() => {
      $("#build-version").textContent = "unknown";
    });
});
