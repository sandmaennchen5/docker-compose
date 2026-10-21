# MultiGit mit Webinterface (Selbstbau)

Der mehrstufige Docker-Build kompiliert [TIVerse/MultiGit](https://github.com/TIVerse/multigit) aus dem Quellcode. Eine eigene schlanke Python-Weboberfläche ruft die CLI auf: Status, Remotes anzeigen, initialisieren, Sync-Vorschau, Sync, Push, Pull und Diagnose. Keine zusätzlichen Python-Pakete erforderlich.

## Starten

```sh
cp .env.example .env
# In .env ein eigenes starkes WEB_PASSWORD setzen.
mkdir -p repos
git clone https://github.com/DEIN-NAME/DEIN-REPO.git repos/mein-repo
docker compose up -d --build
```

Webinterface: http://localhost:8088 – Anmeldung mit `WEB_USER` (Standard `admin`) und dem gesetzten Passwort. Der erste Build kompiliert Rust und kann mehrere Minuten dauern. Für reproduzierbare Builds `MULTIGIT_REF` in `.env` auf einen geprüften Commit oder Tag setzen; Standard ist `master`.

Bestehende Repositories lassen sich alternativ einzeln unter `/repos/<name>` als beschreibbare Bind-Mounts einbinden. Der Container läuft mit UID/GID 1000; auf Linux müssen die eingebundenen Verzeichnisse für diese UID beschreibbar sein. Git-Worktrees mit externem `.git`-Verweis benötigen zusätzlich ihre referenzierten Pfade im Container.

## Einrichten und Zugangsdaten

Im Webinterface zuerst das Repository wählen und initialisieren. Interaktive MultiGit-Einrichtung erfolgt im Terminal, beispielsweise:

```sh
docker compose exec -w /repos/mein-repo multigit mg remote add github DEIN-NAME
docker compose exec -w /repos/mein-repo multigit mg config show
```

Im Container gibt es keinen Desktop-Keyring. MultiGit muss mit seinem verschlüsselten Datei-Backend eingerichtet werden; dessen Verfügbarkeit und Passwortabfragen hängen vom gewählten Upstream-Stand ab. Das persistente Volume `/data` bewahrt Benutzerkonfiguration und Zugangsdaten. Falls die CLI eine interaktive Passwortabfrage benötigt, den betreffenden Befehl über `docker compose exec` ausführen: Das Webinterface liefert bewusst keine Terminaleingaben. SSH-Schlüssel und `known_hosts` bei Bedarf gezielt unter `/data/.ssh` einbinden und passende Dateirechte setzen. Keine Tokens in Remote-URLs speichern.

Webaktionen verwenden feste Argumente ohne Shell, laufen einzeln und haben ein Zeitlimit von 120 Sekunden. Die Ausgabe zeigt CLI-Fehler und Exit-Code. Bei einem Timeout Repository-Status prüfen, bevor erneut synchronisiert wird. Die Vorschau vor einem Sync nutzen; Push/Pull/Sync können lokale oder entfernte Repositories ändern. Remotes hinzufügen und Konflikte lösen erfolgen über die CLI.

## Betrieb

Der Port bindet standardmäßig nur an localhost. Für Zugriff im LAN `BIND_ADDRESS` anpassen; bei Zugriff über das Netzwerk HTTPS über einen Reverse Proxy bereitstellen, da HTTP-Basic-Anmeldung selbst nicht verschlüsselt ist.

```sh
docker compose logs -f
docker compose build --pull --no-cache
docker compose up -d
docker compose down
```

`docker compose down` erhält Daten und Repositories. Es gibt keinen automatischen Sync; Änderungen werden über die Buttons ausgelöst.

## Image von GitHub verwenden

Der Workflow `.github/workflows/multigit-image.yml` testet die Webanwendung und baut das Image für `linux/amd64` und `linux/arm64`. Bei Änderungen an MultiGit auf `main`, bei Tags `multigit-v*` und beim manuellen Start veröffentlicht er nach GHCR. Pull Requests werden nur gebaut. Der aktuelle Upstream-Commit wird vor jedem Build aufgelöst und als Build-Argument verwendet.

Image: `ghcr.io/sandmaennchen5/multigit:latest` (nach erfolgreichem Workflow). Zusätzlich entstehen SHA-Tags und bei Release-Tags beispielsweise `v1.0.0`. Für die Veröffentlichung nutzt GitHub den integrierten `GITHUB_TOKEN`; zusätzliche Registry-Secrets sind nicht nötig.

```sh
docker compose pull
docker compose up -d --no-build
```

Für einen manuellen Build auf GitHub unter **Actions → Build MultiGit image → Run workflow** starten. Falls das GHCR-Paket privat ist, vorher mit `docker login ghcr.io` anmelden oder die Paketsichtbarkeit auf GitHub auf öffentlich stellen.
