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
    apt-get update && apt-get install -y --no-install-recommends \
      fonts-liberation fonts-noto-color-emoji libasound2t64 \
      libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
      libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 \
      libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 \
      libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
      libxfixes3 libxkbcommon0 libxrandr2 libgtk-3-0t64 \
      xvfb xauth && \
    python -m playwright install chromium && \
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
CMD ["sh", "-c", "python -m pricewatch.bootstrap && exec xvfb-run -a uvicorn pricewatch.app:create_app --factory --host 0.0.0.0 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1"]
