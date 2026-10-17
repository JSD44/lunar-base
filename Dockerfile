
# Stage 1: Compile the Lunar Tear grant helper
FROM golang:1.25-bookworm AS grant-builder

ARG LUNAR_TEAR_REF=v1.0.2

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Fetch the Lunar Tear source
RUN curl -fsSL \
    "https://github.com/Walter-Sparrow/lunar-tear/archive/refs/tags/${LUNAR_TEAR_REF}.tar.gz" \
    -o /tmp/lunar-tear.tar.gz && \
    mkdir -p /build/lunar-tear && \
    tar -xzf /tmp/lunar-tear.tar.gz \
    -C /build/lunar-tear --strip-components=1

# Copy the Lunar Base grant shim into the Go module
COPY tools/grant/src/ \
    /build/lunar-tear/server/cmd/lunar-base-grant/

WORKDIR /build/lunar-tear/server

RUN CGO_ENABLED=0 go build \
    -trimpath \
    -o /build/grant \
    ./cmd/lunar-base-grant/


# Stage 2: Python application
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LUNAR_TEAR_DIR=/lunar-tear

WORKDIR /opt/lunar-base

# Install Python dependencies
COPY web/requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir \
    -r /tmp/requirements.txt \
    pycryptodome \
    msgpack \
    lz4

# Copy Lunar Base source
COPY . .

# Copy compiled grant executable
COPY --from=grant-builder \
    /build/grant \
    /opt/lunar-base/tools/grant/grant

# Startup script
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x \
    /usr/local/bin/docker-entrypoint.sh \
    /opt/lunar-base/tools/grant/grant && \
    mkdir -p /opt/lunar-base/data

EXPOSE 8888

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
