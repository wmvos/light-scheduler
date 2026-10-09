ARG BUILD_FROM
FROM $BUILD_FROM

# Install dependencies
RUN apk add --no-cache python3

# Copy application files
COPY rootfs/ /

# Set entrypoint
CMD ["/usr/bin/light_scheduler.py"]
