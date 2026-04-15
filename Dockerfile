FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY requirements.txt /build/requirements.txt

RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r /build/requirements.txt


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd -r appuser && useradd -r -g appuser -u 1001 appuser

COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser src /app/src
COPY --chown=appuser:appuser migrations /app/migrations
COPY --chown=appuser:appuser alembic.ini /app/alembic.ini
COPY --chown=appuser:appuser entrypoint.sh /app/entrypoint.sh

RUN mkdir -p /data && \
    chown appuser:appuser /data && \
    chmod +x /app/entrypoint.sh

VOLUME ["/data"]

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "src.orchestrator"]

HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD python -c "import src.core.config; print('ok')"
