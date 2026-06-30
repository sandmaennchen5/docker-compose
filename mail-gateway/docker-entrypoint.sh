#!/usr/bin/env bash

set -e

echo "======================================"
echo " Mail Gateway starting..."
echo "======================================"

mkdir -p \
    /runtime \
    /runtime/getmail \
    /runtime/msmtp \
    /runtime/status \
    /logs \
    /var/spool/msmtp

touch /runtime/status/status.json

# Ensure correct ownership for non-root scheduler execution
chown -R mail:mail /runtime /logs /var/spool/msmtp 2>/dev/null || true

echo "Checking configuration..."

if [ ! -f /config/config.yaml ]; then
    echo "ERROR: /config/config.yaml not found!"
    exit 1
fi

if [ ! -f /config/accounts.yaml ]; then
    echo "ERROR: /config/accounts.yaml not found!"
    exit 1
fi

echo "Generating runtime configuration..."

python3 /opt/mail-gateway/app/generator.py

echo "Starting Supervisor..."

exec /usr/bin/supervisord -c /opt/mail-gateway/supervisord.conf