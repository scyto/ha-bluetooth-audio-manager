ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM ${BUILD_FROM}

# Pull bluez and mpd from Alpine edge (newer than base 3.20)
RUN apk add --no-cache \
    --repository=https://dl-cdn.alpinelinux.org/alpine/edge/main \
    bluez \
    bluez-btmon \
    bluez-libs \
    && apk add --no-cache \
    --repository=https://dl-cdn.alpinelinux.org/alpine/edge/community \
    mpd \
    && apk add --no-cache \
    dbus-libs \
    pulseaudio-utils \
    python3 \
    py3-pip

COPY docker/requirements.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

RUN mkdir -p /data /root/.config/pulse

COPY docker/rootfs /
COPY src/ /usr/src/

# Fix permissions — COPY from macOS/git can strip execute bits, and
# overlapping directory metadata can affect the base image's /init
RUN chmod +x /init \
    && chmod +x /etc/s6-overlay/s6-rc.d/bt-audio-manager/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/bt-audio-manager/finish \
    && chmod +x /etc/cont-init.d/01-verify-dbus.sh \
    && chmod +x /etc/cont-init.d/02-init-storage.sh \
    && chmod +x /etc/cont-init.d/03-init-mpd.sh

ENV PYTHONPATH="/usr/src"

# Keep add-on in STARTUP state until the web server is listening.
# The Supervisor only transitions to STARTED (showing "Open Web UI")
# once Docker reports the container as healthy.
HEALTHCHECK --interval=5s --timeout=2s --start-period=10s --retries=3 \
  CMD curl -sf http://127.0.0.1:8099/api/health || exit 1

ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_VERSION
ARG BUILD_SHA=""
ENV BUILD_VERSION="${BUILD_VERSION}" \
    BUILD_SHA="${BUILD_SHA}" \
    PYTHONUNBUFFERED="1"
LABEL \
    io.hass.name="Bluetooth Audio Manager" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="Bluetooth Audio Manager" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.version="${BUILD_VERSION}"
