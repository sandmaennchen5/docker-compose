ARG CODE_SERVER_BASE=lscr.io/linuxserver/code-server:latest
FROM ${CODE_SERVER_BASE}

RUN apt-get update && \
    apt-get install -y docker.io docker-compose jq curl git openssh-client nodejs npm python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @devcontainers/cli @unbrained/homeassistant-cli homeassistant-cli typescript lit rollup rollup-plugin-node-resolve rollup-plugin-terser vite eslint prettier home-assistant-js-websocket && \
    pip install --break-system-packages appdaemon wheel pytest ruff black voluptuous && \
    curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash

COPY extensions.txt /config/extensions.txt
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
