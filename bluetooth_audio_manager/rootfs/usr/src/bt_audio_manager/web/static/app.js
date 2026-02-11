/**
 * Bluetooth Audio Manager — Ingress UI
 *
 * Vanilla JS interface with Bootstrap 5.3 components.
 * Communicates with the add-on's REST API via WebSocket for real-time updates.
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
// Section 5: Operation Banner
// ============================================

function showBanner(text) {
  const banner = $("#operation-banner");
  $("#operation-banner-text").textContent = text;
  banner.classList.remove("d-none");
  setButtonsEnabled(false);
}

function hideBanner() {
  const banner = $("#operation-banner");
  banner.classList.add("d-none");
  setButtonsEnabled(true);
}

function setButtonsEnabled(enabled) {
  const btns = [$("#btn-scan"), $("#btn-refresh")];
  btns.forEach((btn) => { if (btn) btn.disabled = !enabled; });
}

// ============================================
// Section 5b: Reconnect Banner & Connection Status
// ============================================

let reconnectTimerId = null;
let reconnectStartTime = null;

function showReconnectBanner() {
  const banner = $("#reconnect-banner");
  banner.classList.add("visible");
  document.body.classList.add("server-unavailable");
  reconnectStartTime = Date.now();

  // Update elapsed time every second
  clearInterval(reconnectTimerId);
  reconnectTimerId = setInterval(() => {
    const elapsed = Math.floor((Date.now() - reconnectStartTime) / 1000);
    const mins = Math.floor(elapsed / 60);
    const secs = elapsed % 60;
    const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
    $("#reconnect-elapsed").textContent = `(${timeStr})`;
  }, 1000);
}

function hideReconnectBanner() {
  const banner = $("#reconnect-banner");
  banner.classList.remove("visible");
  document.body.classList.remove("server-unavailable");
  clearInterval(reconnectTimerId);
  reconnectTimerId = null;
  reconnectStartTime = null;
  $("#reconnect-elapsed").textContent = "";
}

function setConnectionStatus(state) {
  const badge = $("#connection-status");
  if (!badge) return;
  const map = {
    connecting: { text: "Connecting...", cls: "bg-secondary" },
    connected: { text: "Connected", cls: "bg-success" },
    reconnecting: { text: "Reconnecting...", cls: "bg-warning text-dark" },
    disconnected: { text: "Disconnected", cls: "bg-danger" },
  };
  const info = map[state] || map.connecting;
  badge.textContent = info.text;
  badge.className = `badge ${info.cls}`;
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
};

function profileLabels(uuids) {
  if (!uuids || uuids.length === 0) return "";
  const labels = uuids
    .map((u) => BT_PROFILES[u.toLowerCase()])
    .filter(Boolean);
  return labels.length > 0 ? labels.join(", ") : "";
}

// ============================================
// Section 7: Device Rendering (Responsive Grid)
// ============================================

// Cached sinks for merging into device cards
let currentSinks = [];

function renderDevices(devices) {
  const grid = $("#devices-grid");

  if (!devices || devices.length === 0) {
    grid.innerHTML = `
      <div class="col-12">
        <div class="empty-state">
          <i class="fas fa-bluetooth"></i>
          <h5>No Bluetooth audio devices found</h5>
          <p>Put your speaker in pairing mode and click Scan.</p>
        </div>
      </div>
    `;
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
        `;
      }

      // Kebab dropdown for paired/stored devices (Settings + Forget)
      const keepAliveActive = (d.stored || d.paired) && d.keep_alive_active;
      let kebab = "";
      if (d.stored || d.paired) {
        const kaEnabled = d.keep_alive_enabled || false;
        const kaMethod = d.keep_alive_method || "infrasound";
        const safeName = escapeHtml(d.name).replace(/'/g, "\\'");
        kebab = `
          <div class="dropdown">
            <button class="btn btn-sm btn-link text-muted p-0 ms-2" type="button"
                    data-bs-toggle="dropdown" title="Device options">
              <i class="fas fa-ellipsis-v"></i>
            </button>
            <ul class="dropdown-menu dropdown-menu-end">
              <li><a class="dropdown-item" href="#" onclick="openDeviceSettings('${d.address}', '${safeName}', ${kaEnabled}, '${kaMethod}'); return false;">
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

      // Connection detail: bearer type + transport
      let connDetail = "";
      if (d.connected) {
        const parts = [];
        if (d.bearers && d.bearers.length > 0) parts.push(d.bearers.join(" + "));
        if (d.has_transport) parts.push("A2DP");
        if (parts.length > 0) connDetail = parts.join(" / ");
      }

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
          const stateLabel = matchedSink.state === "running" ? "Streaming" : matchedSink.state;
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
                  ${keepAliveActive ? '<i class="fas fa-heartbeat text-danger keep-alive-indicator" title="Keep-alive active"></i>' : ""}
                  <span class="badge ${badgeClass}">${statusText}</span>
                  ${kebab}
                </div>
              </div>
              ${connDetail ? `<div class="small text-muted mb-1">${escapeHtml(connDetail)}</div>` : ""}
              <div class="font-monospace small text-muted">${escapeHtml(d.address)}${rssiDisplay}${d.adapter ? ` on ${escapeHtml(d.adapter)}` : ""}</div>
              ${profiles ? `<div class="small mt-1" style="color: var(--accent-primary)">${escapeHtml(profiles)}</div>` : ""}
              ${sinkInfo}
              <div class="device-actions d-flex gap-2 flex-wrap">
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
      const bleBadge = a.ble_scanning
        ? '<span class="badge bg-warning ms-2">HA BLE Scanning</span>'
        : "";
      const poweredBadge = a.powered
        ? '<span class="badge bg-success">Powered</span>'
        : '<span class="badge bg-secondary">Off</span>';

      const displayName = a.hw_model
        ? `${escapeHtml(a.name)} &mdash; ${escapeHtml(a.hw_model)}`
        : escapeHtml(a.name);

      const selectBtn =
        !a.selected && a.powered
          ? `<button type="button" class="btn btn-sm btn-primary" onclick="selectAdapter('${a.name}')">
               <i class="fas fa-check me-1"></i>Select
             </button>`
          : "";

      return `
        <div class="card adapter-card mb-2">
          <div class="card-body d-flex justify-content-between align-items-center py-2">
            <div>
              <div class="fw-semibold">${displayName}</div>
              <div class="font-monospace small text-muted">${escapeHtml(a.address)}</div>
            </div>
            <div class="d-flex align-items-center gap-2">
              ${poweredBadge}${selectedBadge}${bleBadge}
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

function appendMprisCommand(data) {
  appendEventEntry(
    "mpris",
    `<span class="event-time">${escapeHtml(eventTime(data))}</span>`
    + `<span class="event-type mpris">MPRIS</span>`
    + `<span class="event-content"><strong>${escapeHtml(data.command)}</strong>`
    + (data.detail ? ` <span class="text-muted">${escapeHtml(data.detail)}</span>` : "")
    + `</span>`,
  );
}

function appendAvrcpEvent(data) {
  const valueStr = typeof data.value === "object"
    ? JSON.stringify(data.value)
    : String(data.value);

  appendEventEntry(
    "avrcp",
    `<span class="event-time">${escapeHtml(eventTime(data))}</span>`
    + `<span class="event-type avrcp">AVRCP</span>`
    + `<span class="event-content"><strong>${escapeHtml(data.property)}</strong> = `
    + `<span class="text-success">${escapeHtml(valueStr)}</span></span>`,
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

  const ts = new Date(entry.ts * 1000).toLocaleTimeString();
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

async function refreshDevices() {
  try {
    const [devResult, sinkResult] = await Promise.all([
      apiGet("/api/devices"),
      apiGet("/api/audio/sinks"),
    ]);
    currentSinks = sinkResult.sinks || [];
    renderDevices(devResult.devices);
  } catch (e) {
    showToast(`Refresh failed: ${e.message}`, "error");
  }
}

async function scanDevices() {
  try {
    await apiPost("/api/scan");
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

async function forgetDevice(address) {
  if (!confirm(`Forget device ${address}? This will unpair it.`)) return;
  try {
    await apiPost("/api/forget", { address });
  } catch (e) {
    showToast(`Forget failed: ${e.message}`, "error");
  }
}

async function selectAdapter(adapterName) {
  if (!confirm(`Switch to adapter ${adapterName}? The add-on will restart.`)) return;
  try {
    showBanner(`Switching to adapter ${adapterName}...`);
    const result = await apiPost("/api/set-adapter", { adapter: adapterName });
    if (result.restart_required) {
      showBanner("Restarting add-on with new adapter...");
      await apiPost("/api/restart");
    }
  } catch (e) {
    hideBanner();
    showToast(`Adapter switch failed: ${e.message}`, "error");
  }
}

// ============================================
// Section 11b: Device Settings Modal
// ============================================

let _settingsAddress = null;

function openDeviceSettings(address, name, kaEnabled, kaMethod) {
  _settingsAddress = address;
  $("#device-settings-name").textContent = name;
  $("#device-settings-address").textContent = address;
  $("#setting-keep-alive-enabled").checked = kaEnabled;
  $("#setting-keep-alive-method").value = kaMethod || "infrasound";
  toggleKeepAliveMethodVisibility();
  new bootstrap.Modal("#deviceSettingsModal").show();
}

function toggleKeepAliveMethodVisibility() {
  const enabled = $("#setting-keep-alive-enabled").checked;
  $("#keep-alive-method-group").style.display = enabled ? "" : "none";
}

async function saveDeviceSettings() {
  if (!_settingsAddress) return;
  const settings = {
    keep_alive_enabled: $("#setting-keep-alive-enabled").checked,
    keep_alive_method: $("#setting-keep-alive-method").value,
  };
  try {
    await apiPut(`/api/devices/${encodeURIComponent(_settingsAddress)}/settings`, settings);
    showToast("Device settings saved", "success");
    bootstrap.Modal.getInstance($("#deviceSettingsModal"))?.hide();
  } catch (e) {
    showToast(`Failed to save settings: ${e.message}`, "error");
  }
}

// ============================================
// Section 11c: Add-on Settings Modal
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
// Section 12: WebSocket (Real-time Updates)
// ============================================

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
    hideReconnectBanner();
    setConnectionStatus("connected");
  };

  ws.onmessage = (e) => {
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
      case "status":
        if (msg.message) {
          showBanner(msg.message);
        } else {
          hideBanner();
        }
        break;
      default:
        console.log("[WS] Unknown message type:", msg.type);
    }
  };

  ws.onclose = () => {
    console.log("[WS] Closed, reconnecting in", wsReconnectDelay, "ms");
    ws = null;
    showReconnectBanner();
    setConnectionStatus("reconnecting");
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
  // Set initial connection state
  setConnectionStatus("connecting");

  // Wire up keep-alive toggle in device settings modal
  const kaToggle = $("#setting-keep-alive-enabled");
  if (kaToggle) kaToggle.addEventListener("change", toggleKeepAliveMethodVisibility);

  // WebSocket provides real-time updates (initial state sent on connect)
  connectWebSocket();

  // Load adapter info (once at startup)
  loadAdapters();

  // Show version in header pill and footer
  apiGet("/api/info")
    .then((data) => {
      const ver = `v${data.version}`;
      $("#build-version").textContent = ver;
      $("#version-label").textContent = `${ver} (${data.adapter})`;
    })
    .catch(() => {
      $("#build-version").textContent = "unknown";
    });
});
