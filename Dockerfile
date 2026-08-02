# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# Build tools are only needed while wheels are resolved; the runtime stage
# below starts clean so they never ship in the final image.
FROM base AS builder

WORKDIR /build
COPY requirements.txt .
# No BuildKit cache mount here on purpose: it would make the file unbuildable
# with the classic builder, and CI already caches layers via buildx.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM base AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8000

COPY --from=builder /install /usr/local

WORKDIR /app
COPY inoreader_tagger/ ./inoreader_tagger/
COPY migrate_tags.py ./

# Runs unprivileged so the pod satisfies the cluster's restricted PodSecurity
# profile without needing an exemption.
RUN useradd --uid 10001 --create-home --home-dir /home/tagger tagger \
    && mkdir -p /data \
    && chown -R tagger:tagger /data /app

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"LISTEN_PORT\"]}/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "inoreader_tagger"]
CMD ["serve"]
