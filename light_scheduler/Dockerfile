# https://developers.home-assistant.io/docs/apps/building-docker-image
# syntax=docker/dockerfile:1.4

# This is an example Dockerfile for a Home Assistant Community Add-on.
# We use the official Alpine Linux base image.
# https://developers.home-assistant.io/docs/addons/general-environment

# Default target architecture for this build.
# This can be overridden by using the --build-arg <arch>=<value> on the docker build command.
# https://developers.home-assistant.io/docs/addons/multi-arch
ARG BUILD_FROM=ghcr.io/home-assistant/{arch}-base:3.23
FROM $BUILD_FROM

# Python 3 from the Alpine base. The app is pure-stdlib (no pip), so this is
# the only runtime dependency. We deliberately avoid asyncio/ssl-heavy modules
# to stay within what Alpine's python3 package always ships.
RUN echo "0.2.4" > /tmp/.version && apk add --no-cache python3

# Copy the app into the container.
COPY rootfs /
