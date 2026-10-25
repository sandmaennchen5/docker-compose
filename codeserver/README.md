# 🚀 Custom Code-Server Image (Auto-Updating, Persistent Extensions, DevContainer Support)

Dieses Repository baut ein **erweitertes Code-Server Image**, das automatisch aktualisiert wird, sobald
LinuxServer.io ein neues Code-Server Release veröffentlicht.

Das Image enthält:

- Docker-Client + Docker-Compose
- DevContainer CLI
- Home Assistant Add-on Builder
- Home Assistant Apps Builder
- Persistente VS Code Extensions
- Automatisches Extension-Update bei jedem Start
- Automatische Tag-Versionierung (`latest` + Upstream-Version)
- GitHub Actions Auto-Build nur bei Upstream-Änderungen

---

## ✨ Features

### 🔄 Automatische Upstream-Erkennung
Ein GitHub Action Workflow prüft alle 6 Stunden, ob LinuxServer.io ein neues Code-Server Image veröffentlicht hat.

### 🏷️ Automatische Tag-Versionierung
Wenn ein Update erkannt wird, baut die Action:

- `ghcr.io/<user>/codeserver:latest`
- `ghcr.io/<user>/codeserver:<upstream-version>`

### 🧩 Persistente Extensions
Alle Extensions werden in `/config/extensions.txt` definiert und bei jedem Start aktualisiert.

Die Standardliste liegt im Image unter `/defaults/extensions.txt` und wird beim ersten
Start nach `/config/extensions.txt` kopiert. Installation und laufender Server verwenden
dieselben Verzeichnisse: `/config/extensions` und `/config/data`.

### Deutsche Oberfläche und GitHub-Anmeldung

Das deutsche Sprachpaket wird bei jedem Start installiert. Die Voreinstellung
`locale: de` wird in der code-server-Konfiguration ergänzt, sofern dort noch keine
Sprache gesetzt ist. Nach dem Image-Update den Browser neu laden. Eine explizite
Sprachauswahl im Browser gegebenenfalls über „Configure Display Language“ auf Deutsch setzen.

Für Einstellungen und Anmeldedaten muss **das gesamte `/config`** persistent sein,
beispielsweise im bestehenden Compose-/Portainer-Service:

```yaml
volumes:
  - /pfad/auf/dem/host/code-server:/config
```

Ein Volume nur für den Workspace oder die Extensions genügt nicht. Beim Umstellen
vorhandene `/config`-Daten sichern und übernehmen; ein leeres Volume enthält die
bisherige Anmeldung nicht. Anschließend einmal bei GitHub anmelden und einen
Container-Neustart sowie eine Neuerstellung mit demselben Volume prüfen.

Bleibt die erneute Anmeldung trotz persistentem `/config` erforderlich, muss die
konkrete Anmeldung (z. B. Copilot oder GitHub-Erweiterung) untersucht werden. Auch
Browserdaten und eine gleichbleibende Zugriffs-URL können dabei relevant sein.
Die tatsächlich verwendete Server-/Portainer-Konfiguration liegt diesem Verzeichnis
nicht bei; die Ursache für den Sitzungsverlust ist deshalb noch nicht bestätigt.

### 🛠️ DevContainer & Docker Support
Das Image enthält:

- `docker`
- `docker-compose`
- `devcontainer` CLI

Damit funktionieren DevContainer, Add-on Builder und Apps Builder direkt im Code-Server.

### 📦 Portainer-kompatibel
Portainer erkennt automatisch neue Versionen des Images.

---

## 📁 Repository Struktur

.
├── Dockerfile
├── extensions.txt
├── scripts/
│   ├── install-extensions.sh
│   └── entrypoint.sh
└── .github/
└── workflows/
└── build.yml


---

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
