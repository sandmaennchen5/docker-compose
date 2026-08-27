# mail-gateway

**POP3/IMAP Mail-Gateway für Synology MailPlus**

Holt E-Mails aus externen POP3- und IMAP-Postfächern mit [getmail6](https://getmail6.org/) und leitet sie per [msmtp](https://marlam.de/msmtp/) zuverlässig an einen Synology MailPlus Server weiter.  
MailPlus übernimmt Spamfilter, Antivirus und lokale Zustellung – der Container ist bewusst kein Mailserver.

```
Internet → POP3/IMAP → getmail6 → msmtpq → msmtp → Synology MailPlus :25
```

---

## Inhaltsverzeichnis

- [mail-gateway](#mail-gateway)
  - [Inhaltsverzeichnis](#inhaltsverzeichnis)
  - [Features](#features)
  - [Schnellstart](#schnellstart)
    - [docker-compose.yml](#docker-composeyml)
  - [Konfiguration](#konfiguration)
    - [config.yaml](#configyaml)
    - [accounts.yaml](#accountsyaml)
      - [Konto-Felder](#konto-felder)
  - [Web-Dashboard \& API](#web-dashboard--api)
    - [REST-Endpunkte](#rest-endpunkte)
      - [Beispiele](#beispiele)
  - [Verzeichnisstruktur](#verzeichnisstruktur)
  - [Volumes](#volumes)
  - [Multi-Arch](#multi-arch)
  - [Entwicklung](#entwicklung)
  - [Lizenz](#lizenz)

---

## Features

- **Mehrere Konten** – POP3, POP3S, IMAP, IMAPS beliebig kombinierbar
- **Zero-Config-Betrieb** – nur `config.yaml` + `accounts.yaml` bearbeiten, alle weiteren Configs werden automatisch generiert
- **Robuste Queue** – `msmtpq` puffert Mails lokal, `msmtp-runqueue` sendet bei Verbindungswiederherstellung
- **Bootstrap 5 Dashboard** – Statusübersicht, Konten, Queue, Live-Logs, Action-Buttons
- **REST-API** – programmatischer Zugriff auf Status, Fetch, Queue-Flush, Config-Reload
- **Healthcheck** – Docker-nativer `/health`-Endpunkt
- **Multi-Arch** – `linux/amd64`, `linux/arm64`, `linux/arm/v7` (Synology kompatibel)
- **Kein Root nötig** – soweit getmail6/msmtp es erlauben

---

## Schnellstart

```bash
# 1. Verzeichnis anlegen
mkdir -p mail-gateway/{config,logs,queue}
cd mail-gateway

# 2. Konfiguration anlegen
# Beispiele liegen unter config/config.yaml und config/accounts.yaml im Repo
nano config/config.yaml
nano config/accounts.yaml

# 3. Starten
docker compose up -d

# 4. Web-Dashboard öffnen
# http://<host>:8080
```

### docker-compose.yml

```yaml
services:
  mail-gateway:
    image: ghcr.io/sandmaennche5/docker-compose/mail-gateway:latest
    container_name: mail-gateway
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      TZ: Europe/Berlin
    volumes:
      - ./config:/config      # config.yaml + accounts.yaml
      - ./logs:/logs           # Logdateien
      - ./queue:/var/spool/msmtp  # msmtpq-Spool (Persistenz!)
    healthcheck:
      test: ["CMD", "/opt/mail-gateway/scripts/healthcheck.sh"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
```

---

## Konfiguration

Die gesamte Konfiguration liegt unter `/config/` (Volume-Mount).  
**Generierte Dateien** (`/etc/msmtprc`, `/runtime/getmail/*.conf`) werden bei jedem Start und bei `/api/reload` automatisch überschrieben – niemals manuell bearbeiten.

### config.yaml

```yaml
# Synology MailPlus SMTP-Relay
smtp:
  host: 192.168.1.100   # IP oder Hostname des MailPlus-Servers
  port: 25              # Standard-SMTP (internes Netz, ohne TLS)
  tls: false

  # Optionale SMTP-Auth (MailPlus intern meist nicht nötig)
  # username: relay@example.com
  # password: secret

  # Envelope-From für msmtp
  from: postmaster@example.com

# Abruf-Intervall in Sekunden
poll_interval: 120

# Web-Dashboard Port
web:
  port: 8080
```

### accounts.yaml

Jedes Konto wird in eine eigene getmail6-Konfigurationsdatei übersetzt.  
**Passwörter nicht committen** – die Datei ist in `.gitignore` eingetragen.

```yaml
accounts:

  # POP3S
  - name: firma-pop3        # eindeutiger Name (keine Leerzeichen)
    protocol: pop3s         # pop3 | pop3s | imap | imaps
    host: pop.example.com
    port: 995
    username: user@example.com
    password: "geheimes-passwort"
    deliver_to: user@intern.example.com  # Zieladresse bei MailPlus
    delete: false           # Mails nach Abruf löschen?
    read_all: true          # Auch bereits gelesene Mails abrufen?

  # IMAPS mit mehreren Ordnern
  - name: firma-imap
    protocol: imaps
    host: imap.example.com
    port: 993
    username: user2@example.com
    password: "anderes-passwort"
    deliver_to: user2@intern.example.com
    mailboxes:              # Nur IMAP: abzurufende Ordner
      - INBOX
    delete: false
    read_all: false

  # Office 365
  # - name: o365
  #   protocol: imaps
  #   host: outlook.office365.com
  #   port: 993
  #   username: user@company.com
  #   password: "app-passwort"
  #   deliver_to: user@intern.company.com
  #   mailboxes: [INBOX]
  #   delete: false
  #   read_all: false

  # Gmail (App-Passwort erforderlich)
  # - name: gmail
  #   protocol: imaps
  #   host: imap.gmail.com
  #   port: 993
  #   username: user@gmail.com
  #   password: "xxxx xxxx xxxx xxxx"
  #   deliver_to: user@intern.example.com
  #   mailboxes: [INBOX]
  #   delete: false
  #   read_all: false
```

#### Konto-Felder

| Feld | Pflicht | Standard | Beschreibung |
|---|---|---|---|
| `name` | ✓ | – | Eindeutiger interner Name |
| `protocol` | ✓ | – | `pop3` / `pop3s` / `imap` / `imaps` |
| `host` | ✓ | – | Mailserver-Hostname oder IP |
| `port` | ✓ | – | Serverport |
| `username` | ✓ | – | Login-Name |
| `password` | ✓ | – | Passwort |
| `deliver_to` | – | `username` | Zieladresse bei Synology MailPlus |
| `delete` | – | `false` | Mails nach Abruf vom Server löschen |
| `read_all` | – | `true` | Bereits gelesene Mails ebenfalls abrufen |
| `mailboxes` | – | `[INBOX]` | Nur IMAP: Liste der abzurufenden Ordner |

---

## Web-Dashboard & API

Das Dashboard ist erreichbar unter `http://<host>:8080`.

### REST-Endpunkte

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/health` | Healthcheck (Docker) |
| `GET` | `/api/status` | Status-JSON (letzter Abruf, SMTP, Queue, Uptime) |
| `GET` | `/api/accounts` | Konfigurierte Konten (Passwörter maskiert) |
| `GET` | `/api/logs?file=scheduler.log` | Letzte 200 Zeilen einer Logdatei |
| `GET` | `/api/queue` | Dateiliste der msmtpq-Queue |
| `POST` | `/api/fetch` | Sofortigen Abruf aller Konten triggern |
| `POST` | `/api/runqueue` | Queue sofort an MailPlus senden |
| `POST` | `/api/reload` | Configs aus YAML neu generieren (ohne Neustart) |

#### Beispiele

```bash
# Status abfragen
curl http://localhost:8080/api/status

# Sofortiger Abruf
curl -X POST http://localhost:8080/api/fetch

# Queue leeren
curl -X POST http://localhost:8080/api/runqueue

# Konfiguration neu laden (nach Änderung an config.yaml/accounts.yaml)
curl -X POST http://localhost:8080/api/reload
```

---

## Verzeichnisstruktur

```
mail-gateway/
├── app/
│   ├── config.py        # Konfigurationsmodell (Validierung)
│   ├── generator.py     # Erzeugt msmtprc + getmail-Configs aus YAML
│   ├── scheduler.py     # APScheduler: Polling + runqueue
│   └── web.py           # Flask: Dashboard + REST-API
├── config/
│   ├── config.yaml      # Hauptkonfiguration (SMTP, Intervall, Web)
│   └── accounts.yaml    # Konto-Konfiguration (NICHT committen!)
├── scripts/
│   └── healthcheck.sh   # Docker HEALTHCHECK
├── templates/
│   └── index.html       # Bootstrap 5 Dashboard
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── supervisord.conf
```

**Laufzeit-Verzeichnisse** (Volumes / auto-erstellt):

| Pfad | Inhalt |
|---|---|
| `/config` | `config.yaml`, `accounts.yaml` |
| `/logs` | Rotierende Logdateien (`scheduler.log`, `msmtp.log`, `getmail-<name>.log`, …) |
| `/var/spool/msmtp` | msmtpq-Spool (persistieren für Crash-Sicherheit!) |
| `/runtime/getmail` | Generierte getmail6-Configs + UID-State pro Konto |
| `/runtime/status` | `status.json` (vom Scheduler geschrieben, vom Dashboard gelesen) |

---

## Volumes

Das Volume `/var/spool/msmtp` **muss** persistent sein.  
Wird es gelöscht, gehen alle noch nicht zugestellten E-Mails verloren.

```yaml
volumes:
  - ./queue:/var/spool/msmtp   # Spool unbedingt mounten!
  - ./logs:/logs
  - ./config:/config
```

---

## Multi-Arch

Das Image wird für drei Plattformen gebaut:

| Plattform | Synology-Beispiel |
|---|---|
| `linux/amd64` | DS923+, DS1522+ |
| `linux/arm64` | DS923+ (ARM-Variante), DS220+ |
| `linux/arm/v7` | DS218, DS118 |

Docker wählt beim `docker pull` automatisch die passende Variante.

---

## Entwicklung

```bash
# Lokaler Build
cd mail-gateway
docker build -t mail-gateway:dev .

# Syntax-Check (Python)
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['app/generator.py','app/scheduler.py','app/web.py']]"

# Container mit lokalem Config-Mount starten
docker run --rm -it \
  -p 8080:8080 \
  -v $(pwd)/config:/config \
  -v $(pwd)/logs:/logs \
  -v $(pwd)/queue:/var/spool/msmtp \
  mail-gateway:dev
```

---

## Lizenz

[MIT](../LICENSE) – sandmaennche5
