#!/usr/bin/env bash
set -e

echo "--- Preparing SSH directory ---"
mkdir -p /config/.ssh
chmod 700 /config/.ssh

echo "--- Generating SSH key if missing ---"
if [ ! -f /config/.ssh/id_ed25519 ]; then
  ssh-keygen -t ed25519 -f /config/.ssh/id_ed25519 -N ""
fi

PUBKEY=$(cat /config/.ssh/id_ed25519.pub)

if [ -n "$FORGEJO_TOKEN" ] && [ -n "$FORGEJO_URL" ]; then
  echo "--- Registering SSH key in Forgejo ---"
  curl -s -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: token $FORGEJO_TOKEN" \
    -d "{ \"title\": \"code-server-key\", \"key\": \"$PUBKEY\" }" \
    "$FORGEJO_URL/api/v1/user/keys" || true
else
  echo "--- Skipping Forgejo registration (FORGEJO_TOKEN/URL missing) ---"
fi

echo "--- Installing / updating VS Code extensions ---"
EXT_FILE="/config/extensions.txt"
if [ -f "$EXT_FILE" ]; then
  while IFS= read -r ext || [ -n "$ext" ]; do
    if [ -n "$ext" ]; then
      echo "Extension: $ext"
      code-server --install-extension "$ext" || true
    fi
  done < "$EXT_FILE"
fi

echo "--- Starting code-server ---"
exec code-server --bind-addr 0.0.0.0:8443 /workspace
