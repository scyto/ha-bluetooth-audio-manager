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
    # Run btmon directly to file, kill after 90s
    timeout 90 btmon > "${BTMON_LOG}" 2>&1 &
else
    bashio::log.warning "btmon not found — install 'bluez-btmon' package for AVRCP diagnostics"
fi
