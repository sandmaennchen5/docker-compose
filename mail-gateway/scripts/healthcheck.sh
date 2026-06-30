#!/usr/bin/env bash
# Healthcheck for Mail Gateway.
# Called by Docker every 30 s.  Exits 0 = healthy, 1 = unhealthy.

set -euo pipefail

HEALTH_URL="http://127.0.0.1:${WEB_PORT:-8080}/health"
TIMEOUT=5

if ! curl --silent --fail --max-time "${TIMEOUT}" "${HEALTH_URL}" > /dev/null 2>&1; then
  echo "UNHEALTHY: web endpoint not reachable (${HEALTH_URL})"
  exit 1
fi

echo "HEALTHY"
exit 0
