#!/command/with-contenv bashio
# shellcheck shell=bash
# ==============================================================================
# Initialize MPD directories
# ==============================================================================

mkdir -p /data/mpd/music /data/mpd/playlists

bashio::log.info "MPD directories initialized."
