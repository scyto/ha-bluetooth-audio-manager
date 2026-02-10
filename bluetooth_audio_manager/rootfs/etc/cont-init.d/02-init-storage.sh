#!/command/with-contenv bashio
# shellcheck shell=bash
# ==============================================================================
# Initialize persistent storage
# ==============================================================================

STORE_FILE="/data/paired_devices.json"

if ! bashio::fs.file_exists "${STORE_FILE}"; then
    bashio::log.info "Creating initial paired devices store..."
    echo '{"devices": []}' > "${STORE_FILE}"
fi

bashio::log.info "Persistent storage initialized."
