FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    WEBHOOK_RELAY_CONFIG=/config/config.yaml \
    WEBHOOK_RELAY_DATABASE=/data/webhook-relay.db \
    WEBHOOK_RELAY_HOST=0.0.0.0 \
    WEBHOOK_RELAY_PORT=8080
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
    && useradd --system --uid 10001 --home-dir /data app \
    && mkdir -p /data /config \
    && chown app /data \
    && rm -rf /app/build
USER app
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEBHOOK_RELAY_PORT', '8080'), timeout=2)"]
ENTRYPOINT ["webhook-relay"]
CMD ["serve"]
