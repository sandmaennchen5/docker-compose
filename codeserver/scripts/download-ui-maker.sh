#!/usr/bin/env bash
set -euo pipefail

# Resolve the latest stable release for local builds; CI passes the checked tag.
tag="${UI_MAKER_TAG:-latest}"
endpoint="https://api.github.com/repos/coolshrimp/WinForm-GUI-Maker/releases"
if [[ "$tag" == "latest" ]]; then
  endpoint+="/latest"
else
  endpoint+="/tags/${tag}"
fi

release="$(curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$endpoint")"
asset="$(jq -er '[.assets[] | select(.name | test("^uimaker-.*\\.vsix$"))] | if length == 1 then .[0] else error("Expected one UI Maker VSIX") end' <<< "$release")"
url="$(jq -er '.browser_download_url' <<< "$asset")"
digest="$(jq -r '.digest // empty' <<< "$asset")"
mkdir -p /defaults
curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$url" -o /defaults/ui-maker.vsix
if [[ "$digest" == sha256:* ]]; then
  printf '%s  %s\n' "${digest#sha256:}" /defaults/ui-maker.vsix | sha256sum -c -
fi
jq -er '.tag_name' <<< "$release" > /defaults/ui-maker-version.txt
