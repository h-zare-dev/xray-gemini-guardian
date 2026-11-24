# Bundle the official Xray binary; no Docker socket or host-side Xray needed.
FROM ghcr.io/xtls/xray-core:26.3.27 AS xray

FROM python:3.12-bookworm
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    GUARDIAN_DATA_DIR=/data \
    HOME=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb fluxbox x11vnc novnc websockify tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt \
    && python -m playwright install --with-deps chrome \
    && rm -rf /root/.cache

COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
COPY guardian /app/guardian
COPY legacy /app/legacy
COPY docker /app/docker
RUN useradd --system --uid 10001 --create-home --home-dir /home/guardian guardian \
    && mkdir -p /data /app/legacy/src \
    && chown -R guardian:guardian /data /home/guardian \
    && chmod +x /app/docker/entrypoint.sh \
    && test -x /usr/local/bin/xray \
    && test -x /usr/bin/google-chrome

USER guardian
EXPOSE 6080
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
