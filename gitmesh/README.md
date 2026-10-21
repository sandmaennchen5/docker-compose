# GitMesh

Eigenständiger Git-Multisync-Dienst mit deutschem Dashboard für GitHub, GitLab, Forgejo und andere Git-Server. Python-Standardbibliothek, Git CLI und SQLite; keine zusätzliche Datenbank nötig.

## Start

1. `.env.example` nach `.env` kopieren und zwei unabhängige zufällige Secrets (mindestens 32 Zeichen) eintragen.
2. Für private Repositories SSH-Schlüssel, SSH-Konfiguration und geprüfte `known_hosts` unter `ssh/` bereitstellen und den auskommentierten Mount aktivieren. UID 10001 benötigt Leserechte. Schlüssel mit minimalen Schreibrechten pro Repository verwenden. Keine Tokens in URLs eintragen.
3. Lokal bauen: `docker compose up -d --build` im Verzeichnis `gitmesh`.
4. Fertiges GitHub-Image verwenden: `docker compose -f docker-compose.ghcr.yml pull` und anschließend `docker compose -f docker-compose.ghcr.yml up -d`. Diese Datei ist auch für Portainer gedacht. Das Image muss zuvor durch den GitHub-Workflow veröffentlicht worden sein.
5. http://localhost:8088 öffnen, mit dem Admin-Token anmelden und unter **Einstellungen** Netze hinzufügen, Remote-URLs eintragen und speichern. Mindestens zwei Remotes pro Netz. Intervall, Aktivierung und Webhook-Secret-Umgebungsvariable sind ebenfalls im Webinterface einstellbar.

Das Admin-Token bleibt im Browser-Arbeitsspeicher. SSH-Schlüssel und Bootstrap-Secrets werden weiterhin über Mounts bzw. Umgebungsvariablen bereitgestellt.

GitMesh startet mit einer leeren Konfiguration. Einstellungen werden atomar unter `/data/gitmesh.json` im Volume `gitmesh-data` gespeichert und bleiben bei Container-Updates erhalten. Kein Neustart nötig. Während eines Syncs wird Speichern mit einem Hinweis abgelehnt; anschließend erneut speichern. Entfernte Netze behalten lokale Git-Daten und Historie. Automatische Dashboard-Updates überschreiben keine ungespeicherten Formulare.

Migration von der ersten Version: Die bisherige `gitmesh.json` bei gestopptem Dienst als `/data/gitmesh.json` in das Datenvolume kopieren oder die Netze einmalig im Webinterface anlegen. Den alten schreibgeschützten `/config/gitmesh.json`-Mount entfernen. Für einen benutzerdefinierten Pfad `GITMESH_CONFIG` setzen; dessen Verzeichnis muss beschreibbar sein. Die Beispieldatei ist weiterhin als Importvorlage für Administratoren verwendbar.

Der Container läuft ohne Root mit schreibgeschütztem Root-Dateisystem. Vor Backups/Restore den Dienst stoppen und das gesamte Volume sichern/wiederherstellen.

## Automatischer GitHub-Build

Der Workflow `.github/workflows/gitmesh-image.yml` führt zuerst alle Tests mit Python 3.13 aus. Anschließend baut er für `linux/amd64` und `linux/arm64` und veröffentlicht `ghcr.io/<owner>/gitmesh`:

- Pushes mit GitMesh-Änderungen auf `main`: `latest` und `sha-<vollständiger-commit>`.
- Tags wie `gitmesh-v1.0.0`: `v1.0.0` und Commit-Tag; `latest` bleibt unverändert.
- Jeden Montag um 04:43 UTC sowie manuell über **Actions → Build GitMesh image → Run workflow**: erneuter Build, damit aktualisierte Basis-Images berücksichtigt werden.
- Pull Requests: Tests und Image-Build ohne Veröffentlichung und ohne Registry-Anmeldung.

Der Workflow wird nach Commit und Push aktiv. Er verwendet `GITHUB_TOKEN` mit `packages: write`; kein zusätzliches Registry-Secret erforderlich. Repository-Einstellungen müssen Actions und das Schreiben von Packages erlauben. Ein neu angelegtes GHCR-Package gegebenenfalls auf **Public** stellen oder den Docker-Host an GHCR anmelden. `GITMESH_IMAGE` erlaubt in beiden Compose-Dateien einen anderen Owner oder festen Image-Tag. Laufende Container werden nicht automatisch aktualisiert; neue Images mit `pull` und `up -d` übernehmen.

## Synchronisierung

Alle Remotes werden zuerst abgefragt. Pro Branch wird eine vorhandene Spitze gesucht, die alle anderen Spitzen als Vorfahren enthält. Nur dieser Stand wird per gewöhnlichem Push verteilt. Gleichstände erzeugen keine Pushes: Rückkopplungen durch eigene Webhooks enden von selbst. Unterschiedliche Tag-Objekte und divergierende Branches werden als Konflikt angezeigt; andere konfliktfreie Referenzen können weiterlaufen. Parallele Änderungen schützt Gits Fast-forward-Prüfung. Fehler stoppen den Lauf; bereits erfolgreiche Pushes werden beim nächsten Lauf berücksichtigt.

Keine automatischen Merges, Force-Pushes oder Löschweitergabe. Ein auf einem Remote gelöschter Branch wird aus anderen wiederhergestellt. Zum vollständigen Löschen das Netz deaktivieren und die Referenz auf allen Remotes löschen. Konflikte lokal mergen und das Ergebnis pushen; der nächste Lauf verteilt den Merge. Branchschutz und Serverberechtigungen gelten unverändert.

## Webhooks und Betrieb

Optional Push-Webhooks auf `https://DEIN-HOST/hooks/github/NETZNAME`, `/hooks/gitlab/NETZNAME` oder `/hooks/forgejo/NETZNAME` manuell einrichten. Als Secret den Wert der im Netz unter `webhook_secret_env` genannten Umgebungsvariable verwenden. GitHub und Forgejo werden mit HMAC-SHA256 geprüft, GitLab mit dem Secret-Header. Hooks prüfen ausschließlich konfigurierte Remotes; Payload-URLs werden nicht übernommen. Polling im eingestellten Intervall (standardmäßig 300 Sekunden) gleicht verlorene oder während eines Laufs zusammengefasste Events aus. Eine einzelne Dienstinstanz betreiben.

Für externen Zugriff einen HTTPS-Reverse-Proxy mit Request-Limits und Zeitlimits vorschalten. Standardbindung ausschließlich localhost. Verwaltungs-API mit Bearer-Authentifizierung; `/healthz` öffentlich. Remote-URLs und SSH-Konfiguration nur durch vertrauenswürdige Administratoren verwalten.

## Umfang dieser ersten Version

Enthalten: Multi-Repository-Konfiguration, bidirektionale Branch- und Tag-Synchronisierung, Dashboard, Einstellungen im Webinterface, manuelles Auslösen, geprüfte Webhooks, Polling, Konfliktanzeige und persistente Historie.

Noch nicht enthalten: Repository-Discovery, automatische Webhook-Registrierung, PAT-Tresor/OAuth, Benutzerrollen, Issues, Pull Requests, Releases, Wikis und Git LFS. Private Repositories werden über SSH-Schlüssel angebunden. Submodule synchronisieren nur Git-Verweise; ihre Repositories benötigen eigene Netze. Historie wird derzeit dauerhaft aufbewahrt.

## Tests

`python -m unittest discover -s tests -v`

Integrationstests verwenden drei temporäre lokale Git-Repositories. Ausschließlich der Test-Runner erlaubt dafür Dateitransport; der Dienst akzeptiert HTTPS und SSH.
