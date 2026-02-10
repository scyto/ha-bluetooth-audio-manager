#!/command/with-contenv bashio
# shellcheck shell=bash
# ==============================================================================
# Start btmon capture for AVRCP diagnostics (runs for 90s at startup)
# ==============================================================================

BTMON_LOG="/data/btmon_startup.log"

if command -v btmon >/dev/null 2>&1; then
    bashio::log.info "Starting btmon capture to ${BTMON_LOG} (90s)..."
    # Rotate previous capture
    [ -f "${BTMON_LOG}" ] && mv "${BTMON_LOG}" "${BTMON_LOG}.prev"
    # Run btmon in background, auto-kill after 90s
    (timeout 90 btmon 2>&1 | head -n 5000 > "${BTMON_LOG}") &
else
    bashio::log.warning "btmon not found — install 'bluez' package for AVRCP diagnostics"
fi
