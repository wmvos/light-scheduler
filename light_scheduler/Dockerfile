# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# Python 3 from the Alpine base. The app is pure-stdlib (no pip), so this is
# the only runtime dependency. We deliberately avoid asyncio/ssl-heavy modules
# to stay within what Alpine's python3 package always ships.
RUN apk add --no-cache python3

# Copy the app into the container.
COPY rootfs /

# Ensure the s6 service scripts and the program are executable.
RUN chmod +x \
    /etc/services.d/light_scheduler/run \
    /etc/services.d/light_scheduler/finish \
    /usr/bin/light_scheduler.py

LABEL \
    org.opencontainers.image.title="Home Assistant App: Light Scheduler" \
    org.opencontainers.image.description="Schedule on/off, brightness and color temperature of your lights on a visual daily/weekly timeline." \
    org.opencontainers.image.source="https://github.com/wmvos/light-scheduler" \
    org.opencontainers.image.licenses="GPL-3.0-only"
