#!/bin/sh
set -eu

if [ -z "${PRICEWATCH_APP_SECRET_KEY:-}" ]; then
    PRICEWATCH_APP_SECRET_KEY="$(python -m pricewatch.runtime_secret)"
    export PRICEWATCH_APP_SECRET_KEY
fi

python -m pricewatch.bootstrap

exec python -m pricewatch.runtime
