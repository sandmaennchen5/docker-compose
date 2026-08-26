#!/bin/sh
set -eu
PIDS=""

log(){ printf '%s\n' "[universal-agent] $*"; }

is_enabled(){
  case "${1:-}" in true|TRUE|1|yes|YES|on|ON) return 0;; *) return 1;; esac
}

read_secret(){
  direct="${1:-}"; file="${2:-}"
  if [ -n "$file" ]; then
    [ -r "$file" ] || { log "ERROR: Secret file not readable: $file"; return 1; }
    cat "$file"
  else
    printf '%s' "$direct"
  fi
}

shutdown(){
  trap - TERM INT EXIT
  for pid in $PIDS; do kill -TERM "$pid" 2>/dev/null || true; done
  wait || true
}
trap shutdown TERM INT EXIT

health(){
  found=0
  if is_enabled "${ENABLE_PORTAINER:-true}"; then found=1; pgrep -f '/usr/local/bin/portainer-agent' >/dev/null || return 1; fi
  if is_enabled "${ENABLE_HAWSER:-true}"; then found=1; pgrep -f '/usr/local/bin/hawser' >/dev/null || return 1; fi
  if is_enabled "${ENABLE_PORTWING:-true}"; then found=1; pgrep -f '/usr/local/bin/portwing' >/dev/null || return 1; fi
  [ "$found" -eq 1 ]
}

if [ "${1:-}" = health ]; then health; exit $?; fi

PORTAINER_SECRET="$(read_secret "${PORTAINER_AGENT_SECRET:-}" "${PORTAINER_AGENT_SECRET_FILE:-}")"
HAWSER_TOKEN_VALUE="$(read_secret "${HAWSER_TOKEN:-}" "${HAWSER_TOKEN_FILE:-}")"

if is_enabled "${ENABLE_PORTAINER:-true}"; then
  env AGENT_SECRET="$PORTAINER_SECRET" /usr/local/bin/portainer-agent &
  PIDS="$PIDS $!"
fi

if is_enabled "${ENABLE_HAWSER:-true}"; then
  env PORT="${HAWSER_PORT:-2376}" TOKEN="$HAWSER_TOKEN_VALUE" \
      STACKS_DIR="${HAWSER_STACKS_DIR:-/data/hawser/stacks}" \
      DOCKER_SOCKET="${HAWSER_DOCKER_SOCKET:-/var/run/docker.sock}" \
      SERVER_URL="${HAWSER_SERVER_URL:-}" \
      TLS_CERT="${HAWSER_TLS_CERT:-}" TLS_KEY="${HAWSER_TLS_KEY:-}" \
      /usr/local/bin/hawser &
  PIDS="$PIDS $!"
fi

if is_enabled "${ENABLE_PORTWING:-true}"; then
  if [ -n "${PORTWING_TOKEN_FILE:-}" ]; then
    env PORT="${PORTWING_PORT:-3000}" TOKEN_FILE="${PORTWING_TOKEN_FILE}" \
        STACKS_DIR="${PORTWING_STACKS_DIR:-/data/portwing/stacks}" \
        DOCKER_SOCKET="${PORTWING_DOCKER_SOCKET:-/var/run/docker.sock}" \
        TLS_CERT="${PORTWING_TLS_CERT:-}" TLS_KEY="${PORTWING_TLS_KEY:-}" \
        /usr/local/bin/portwing &
  else
    env PORT="${PORTWING_PORT:-3000}" TOKEN="${PORTWING_TOKEN:-}" \
        STACKS_DIR="${PORTWING_STACKS_DIR:-/data/portwing/stacks}" \
        DOCKER_SOCKET="${PORTWING_DOCKER_SOCKET:-/var/run/docker.sock}" \
        TLS_CERT="${PORTWING_TLS_CERT:-}" TLS_KEY="${PORTWING_TLS_KEY:-}" \
        /usr/local/bin/portwing &
  fi
  PIDS="$PIDS $!"
fi

[ -n "$PIDS" ] || { log "ERROR: No agents enabled."; exit 1; }

while true; do
  for pid in $PIDS; do
    kill -0 "$pid" 2>/dev/null || { log "ERROR: Agent process $pid terminated."; exit 1; }
  done
  sleep 5
done
