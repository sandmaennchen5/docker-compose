# docker-compose

Docker Images & Compose Stacks von **sandmaennche5**.  
Alle Images werden automatisch über GitHub Actions gebaut und auf [GHCR](https://ghcr.io/sandmaennche5/docker-compose) veröffentlicht.

---

## Images

| Image | Beschreibung | Basis |
|---|---|---|
| [codeserver](./codeserver/) | Erweitertes Code-Server Image mit Docker, DevContainer CLI und persistenten Extensions | `lscr.io/linuxserver/code-server` |
| [mail-gateway](./mail-gateway/) | POP3/IMAP Mail-Gateway für Synology MailPlus | `python:3.12-slim-bookworm` |

---

## codeserver

> `ghcr.io/sandmaennchen5/docker-compose/codeserver:latest`

Erweitertes [LinuxServer Code-Server](https://github.com/linuxserver/docker-code-server) Image.  
Wird automatisch neu gebaut, sobald LinuxServer.io ein neues Release veröffentlicht.

**Enthält:**
- Docker-Client & Docker Compose
- DevContainer CLI (`@devcontainers/cli`)
- Home Assistant Add-on / Apps Builder Tools
- Persistente VS Code Extensions (via `extensions.txt`)
- Automatisches Extension-Update bei jedem Start
- Forgejo SSH-Key-Registrierung (optional via ENV)

**Quick Start:**

```yaml
services:
  codeserver:
    image: ghcr.io/sandmaennchen5/docker-compose/codeserver:latest
    container_name: codeserver
    restart: unless-stopped
    ports:
      - "8443:8443"
    volumes:
      - ./config:/config
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      TZ: Europe/Berlin
      PASSWORD: changeme
```

→ Details: [codeserver/README.md](./codeserver/) (sofern vorhanden) oder [`codeserver/Dockerfile`](./codeserver/Dockerfile)

---

## mail-gateway

> `ghcr.io/sandmaennchen5/docker-compose/mail-gateway:latest`

Holt E-Mails aus POP3- und IMAP-Postfächern mit **getmail6** und leitet sie per **msmtp** an einen **Synology MailPlus** Server weiter.  
MailPlus übernimmt Spamfilter, Antivirus und lokale Zustellung.

**Features:**
- Mehrere Konten (POP3/POP3S/IMAP/IMAPS)
- Konfiguration ausschließlich über `config.yaml` + `accounts.yaml`
- Robuste Queue via `msmtpq` / `msmtp-runqueue`
- Bootstrap 5 Web-Dashboard (Port 8080)
- REST-API (`/api/status`, `/api/fetch`, `/api/reload`, …)
- Multi-Arch: `linux/amd64`, `linux/arm64`, `linux/arm/v7`
- Healthcheck via `/health`

**Quick Start:**

```bash
cd mail-gateway
cp config/config.yaml ./config/config.yaml      # Pfade anpassen
cp config/accounts.yaml ./config/accounts.yaml  # Konten eintragen
docker compose up -d
# WebUI: http://localhost:8080
```

→ Details: [`mail-gateway/`](./mail-gateway/)

---

## Lizenz

[MIT](./LICENSE) – sandmaennche5

## 🐳 Dockerfile

Das Dockerfile basiert auf dem neuesten LinuxServer Code-Server Image und installiert zusätzliche Tools.

```Dockerfile
FROM lscr.io/linuxserver/code-server:latest

RUN apt-get update && \
    apt-get install -y docker.io docker-compose jq && \
    npm install -g @devcontainers/cli @home-assistant/add-on-builder @home-assistant/apps-builder && \
    rm -rf /var/lib/apt/lists/*

COPY extensions.txt /config/extensions.txt
COPY scripts/install-extensions.sh /usr/local/bin/install-extensions
RUN chmod +x /usr/local/bin/install-extensions

COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
