ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    bluez-libs \
    dbus-libs \
    pulseaudio-utils \
    python3 \
    py3-pip

COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

RUN mkdir -p /data

COPY rootfs /

# Fix permissions — COPY from macOS/git can strip execute bits, and
# overlapping directory metadata can affect the base image's /init
RUN chmod +x /init \
    && chmod +x /etc/s6-overlay/s6-rc.d/bt-audio-manager/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/bt-audio-manager/finish \
    && chmod +x /etc/cont-init.d/01-verify-dbus.sh \
    && chmod +x /etc/cont-init.d/02-init-storage.sh

ENV PYTHONPATH="/usr/src"

ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_VERSION
LABEL \
    io.hass.name="Bluetooth Audio Manager" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="Bluetooth Audio Manager" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.version="${BUILD_VERSION}"
