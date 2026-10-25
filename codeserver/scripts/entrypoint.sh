#!/usr/bin/env bash
set -e

echo "--- Preparing persistent code-server data ---"
mkdir -p /config/data /config/extensions
export CODE_SERVER_CONFIG="${CODE_SERVER_CONFIG:-/config/.config/code-server/config.yaml}"
mkdir -p "$(dirname "$CODE_SERVER_CONFIG")"
touch "$CODE_SERVER_CONFIG"
if ! grep -qE '^[[:space:]]*locale[[:space:]]*:' "$CODE_SERVER_CONFIG"; then
  printf '\nlocale: de\n' >> "$CODE_SERVER_CONFIG"
fi

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
if [ ! -f "$EXT_FILE" ]; then
  cp /defaults/extensions.txt "$EXT_FILE"
fi
if [ -f "$EXT_FILE" ]; then
  while IFS= read -r ext || [ -n "$ext" ]; do
    if [ -n "$ext" ]; then
      echo "Extension: $ext"
      code-server --user-data-dir /config/data --extensions-dir /config/extensions --install-extension "$ext" || true
    fi
  done < "$EXT_FILE"
fi

# Install the language pack even when an existing volume has its own extension list.
code-server --user-data-dir /config/data --extensions-dir /config/extensions \
  --install-extension MS-CEINTL.vscode-language-pack-de || true

echo "--- Handing over to LinuxServer init ---"
exec /init
