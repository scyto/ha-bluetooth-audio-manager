#!/command/with-contenv bashio
# shellcheck shell=bash
# ==============================================================================
# Verify D-Bus socket is accessible (required for BlueZ communication)
# ==============================================================================

if ! bashio::fs.socket_exists "/run/dbus/system_bus_socket"; then
    bashio::log.fatal "D-Bus system socket not found at /run/dbus/system_bus_socket"
    bashio::log.fatal "Ensure 'host_dbus: true' is set in config.yaml"
    bashio::exit.nok
fi

bashio::log.info "D-Bus socket verified."
