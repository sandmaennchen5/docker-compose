FROM lscr.io/linuxserver/code-server:latest

# Tools installieren (einmalig im Image)
RUN apt-get update && \
    apt-get install -y docker.io docker-compose jq curl git openssh-client nodejs npm python3 python3-pip golang && \
    rm -rf /var/lib/apt/lists/*
# Dev-/HA-Tools (wenn du willst, hier noch ergänzen)
RUN npm install -g @devcontainers/cli @unbrained/homeassistant-cli homeassistant-cli typescript lit rollup rollup-plugin-node-resolve rollup-plugin-terser vite eslint prettier home-assistant-js-websocket && \
    pip install  --break-system-packages appdaemon wheel pytest ruff black voluptuous \
    go install github.com/nektos/act@latest
    
# Extensions-Liste und Entrypoint
COPY extensions.txt /config/extensions.txt
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
