#!/command/with-contenv bashio
# shellcheck shell=bash
# ==============================================================================
# Initialize MPD directories
# ==============================================================================

mkdir -p /data/mpd/music /data/mpd/playlists
chown -R mpd:mpd /data/mpd

bashio::log.info "MPD directories initialized."
