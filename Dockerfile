FROM python:3.12-slim-trixie AS wheels
WORKDIR /build
COPY pyproject.toml constraints.txt ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels --constraint constraints.txt .

FROM python:3.12-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    PRICEWATCH_DATA_DIR=/data \
    PRICEWATCH_DATABASE_URL=sqlite:////data/pricewatch.db \
    PYTHONPATH=/app/src
WORKDIR /app
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels pricewatch && \
    python -m playwright install --with-deps --only-shell chromium && \
    groupadd --gid 10001 pricewatch && \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /home/pricewatch pricewatch && \
    mkdir -p /data && chown 10001:10001 /data && \
    rm -rf /wheels /var/lib/apt/lists/*
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY LICENSE THIRD_PARTY_NOTICES.md ./
USER 10001:10001
EXPOSE 8080
CMD ["sh", "-c", "python -m pricewatch.bootstrap && exec uvicorn pricewatch.app:create_app --factory --host 0.0.0.0 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1"]
