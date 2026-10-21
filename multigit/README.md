# MultiGit mit vollständigem Webinterface

Das Image kompiliert [TIVerse/MultiGit](https://github.com/TIVerse/multigit) und ergänzt ein deutsches Webinterface. Häufige Aufgaben haben Statusansichten und Formulare; sämtliche nativen Befehle einschließlich interaktiver Menüs sind über einen Funktionskatalog mit eingebettetem Terminal erreichbar.

## Start mit einem GitHub-Image

Im Verzeichnis multigit:

~~~sh
cp .env.example .env
# WEB_PASSWORD auf ein eigenes starkes Passwort setzen.
mkdir -p repos
docker compose pull
docker compose up -d --no-build
~~~

Image: ghcr.io/sandmaennchen5/multigit:latest. Die erweiterten Funktionen sind erst nach dem Bau und Bereitstellen des neuen Images verfügbar. Für einen lokalen Selbstbau stattdessen docker compose up -d --build ausführen. Rust wird dabei kompiliert; der erste Build kann mehrere Minuten dauern. MULTIGIT_REF wählt einen Upstream-Commit oder Tag, Standard master.

Standardadresse: **http://localhost:8088**. Benutzer: WEB_USER (Standard admin), Passwort: WEB_PASSWORD.

### NAS / Dockhand / Portainer

Der Webserver verwendet immer **Container-Port 8080**. Ein eigener NAS-Port wird links angegeben:

~~~yaml
ports:
  - "55567:8080"
~~~

Danach den Stack **neu bereitstellen** und **http://NAS-IP:55567** öffnen. Ein Container-Neustart allein ändert Portzuordnungen nicht. Die mitgelieferte Compose-Datei bindet standardmäßig an localhost; für Zugriff über die NAS-IP BIND_ADDRESS=0.0.0.0 setzen. Für Zugriff über das Netzwerk HTTPS über einen Reverse Proxy verwenden, damit die Basic-Anmeldung verschlüsselt übertragen wird.

## Oberfläche

| Bereich | Funktionen |
|---|---|
| Status | Branch, HEAD, Arbeitsverzeichnis, geänderte Dateien, Konflikte, bekannte Remote-Abstände, lokale Branches, Tags, letzte 30 Commits, Version und Keyring-Status |
| Repository hinzufügen | Lokales Repository anlegen oder HTTPS-/SSH-Repository nach /repos klonen |
| Alle Funktionen | Durchsuchbarer Katalog aller nativen MultiGit-Befehle mit Argumenten und Befehlshilfe |
| Terminal / Sitzungen | Echte Linux-PTY mit Farben, Pfeiltasten, Passwortabfragen, Menüs, Enter, Strg+C, Größenänderung, Abbruch und Wiederverbinden |
| Einstellungen / Remotes | Branch für Push/Sync, Pull-Quelle, Parallelität, Log-Stufe sowie Git-URLs und Anbieter einrichten und bearbeiten |
| Vollständige Konfiguration | Repository- und globale TOML-Datei mit allen nativen Feldern, Datentypprüfung, Sicherung und Erkennung gleichzeitiger Änderungen |

Status wird auf Wunsch alle zehn Sekunden aktualisiert. Remote-Abstände beziehen sich auf **lokal bekannte** Referenzen; Fetch aktualisiert diese. Die Oberfläche löst beim Anzeigen keinen Netzwerk-Sync aus. Höchstens 200 geänderte Dateien erscheinen in der Übersicht; der native Status zeigt weitere Details.

### Native Funktionen im Browser

- **Repository und Remotes:** Initialisierung, Einrichtungsassistent, Plattform-Repository erstellen, Remotes auflisten/hinzufügen/entfernen/testen und Zugangsdaten aktualisieren.
- **Synchronisieren:** Fetch, Push, Pull, Sync mit Vorschau, Merge, Backup und Mirror mit Vorschau. Native Optionen einschließlich Force können über das Argumentfeld gesetzt werden.
- **Branches und Tags:** Anzeigen, erstellen, löschen, wechseln und native Optionen für Basisbranch, Tag-Nachricht und Signierung.
- **Dateien und Commits:** Interaktives Staging, Conventional-Commit-Assistent, Commit, Amend, Diff, Show, Verlauf/Graph, Changelog, Blame und Suche.
- **Änderungen und Konflikte:** Stash, Undo, Reset, Revert, Cherry-pick, Rebase, Konflikterkennung und native Konfliktauflösung.
- **Daemon:** Start mit Intervall, Stop, Status und Protokoll.
- **Weitere Werkzeuge:** TUI-Dashboard, Arbeitssitzungen, Templates, Hooks, Aliase, Statistik, Diagnose/Reparatur, Version und Hilfe.
- **Git-Durchreichung:** Weitere Git-Befehle wie config user.name "Mein Name", config user.email "name@example.com" oder status über die native Durchreichung ausführen.

Ein Werkzeug wählen, Argumente optional ergänzen und starten. --help zeigt die verfügbaren Optionen des **tatsächlich gebauten MultiGit-Stands**. Argumente werden ohne Shell an MultiGit übergeben. Interaktive Programme laufen direkt im Terminal; als Editor ist Nano installiert. Nano und GPG sind installiert; eigene Signierschlüssel müssen für die native Tag-/Commit-Signierung verfügbar sein.

Die Oberfläche stellt die Funktionen der Basis bereit. Unvollständige Upstream-Implementierungen, native Standardwerte und Fehlermeldungen bleiben sichtbar; sie werden nicht durch simulierte Erfolgsmeldungen ersetzt. Die Basis behandelt beispielsweise Sync-Strategien und manche Konfigurationsfelder nicht in jedem Befehl gleich.

## Repositories und Einstellungen

Das Verzeichnis ./repos ist nach /repos eingebunden. Ein Repository liegt beispielsweise unter /repos/mein-projekt. Bestehende Repositories lassen sich auch einzeln als beschreibbare Bind-Mounts einbinden. Der Container läuft als UID/GID **1000**; auf Linux müssen die Volume-Rechte dazu passen.

Repository-Einstellungen stehen in .multigit/config.toml; globale Vorgaben in /data/config/multigit/config.toml. Vor Änderungen wird eine config.toml.bak angelegt. Der einfache Formulareditor erhält unbekannte Felder, entfernt beim Neuformatieren aber TOML-Kommentare. Der vollständige Editor erhält den eingegebenen Text und weist veraltete Schreibstände zurück.

Lokale, unversionierte Konfiguration wird über .git/info/exclude vom Sync ausgeschlossen. Bereits versionierte Konfiguration bleibt eine normale Git-Änderung. Der Einstellungseditor benötigt die Git-Verwaltung innerhalb des Repository-Verzeichnisses; externe Git-Verwaltung/Worktrees werden dort abgelehnt. Native Befehle können Worktrees verwenden, sofern alle referenzierten Pfade korrekt im Container verfügbar sind.

Ein deaktiviertes MultiGit-Ziel bleibt als Git-Remote erhalten. Die Pull-Quelle wird ausdrücklich gewählt; Pull verwendet den aktuell ausgecheckten Branch. Speichern einer Konfiguration startet keinen Sync und keinen Daemon. Die Vorschau zusammengeführter Konfiguration zeigt die Web-Zusammenführung; die native CLI hat teilweise andere Zusammenführungsregeln.

## Token-Keyring und SSH

Das Image startet eine private D-Bus-Sitzung mit GNOME Secret Service. Dadurch können native MultiGit-Assistenten Tokens in ihrem vorgesehenen System-Keyring speichern, auch ohne Desktop auf dem NAS. Beim Start werden Speichern, Lesen und Löschen eines geheimnisfreien Testeintrags geprüft.

Der Keyring liegt verschlüsselt im persistenten Volume /data. Als Masterpasswort dient KEYRING_PASSWORD, falls gesetzt, sonst WEB_PASSWORD. **Dieses Masterpasswort bei bestehenden Daten beibehalten.** Soll nur das Webpasswort geändert werden, zunächst KEYRING_PASSWORD auf das bisherige Masterpasswort setzen. Ein falsches Masterpasswort verhindert den Start und wird im Container-Log gemeldet. Passwörter müssen einzeilig sein.

Zugangsdaten in den nativen Einrichtungs-/Remote-Assistenten im Browser-Terminal eingeben. Die alternative Eingabezeile ist verdeckt. Tokens gehören nicht in Git-URLs oder TOML-Dateien. Die Oberfläche protokolliert Terminaleingaben nicht und speichert Terminalausgaben nur begrenzt im Arbeitsspeicher.

Für SSH Schlüssel, SSH-Konfiguration und verifizierte known_hosts unter /data/.ssh einbinden. Eine persistente Identität bzw. ein vorhandener Agent muss für SSH verfügbar sein. Native HTTPS- und SSH-Unterstützung hängt zusätzlich von den Upstream-Befehlen ab.

## Sitzungen und Betrieb

Bis zu vier native Sitzungen können gleichzeitig laufen; davon ein schreibender Befehl. Während eines solchen Befehls sind Einstellungsänderungen gesperrt. Lesende Befehle ohne zusätzliche Optionen bleiben möglich. Zusätzliche Argumente werden vorsichtig als schreibend eingestuft; Befehlshilfe bleibt lesend. Der explizite Daemon-Stop ist auch bei laufendem Daemon verfügbar.

Laufende Sitzungen sind nach dem Neuladen wieder auswählbar. Ausgabe ist auf etwa eine Million Zeichen pro Sitzung begrenzt. Unbenutzte normale Sitzungen enden nach 30 Minuten. Ein explizit gestarteter Auto-Sync ist davon ausgenommen und läuft bis Stop oder Container-Neustart. Nach einem Container-Neustart muss er erneut gestartet werden. Die Oberfläche startet keinen Auto-Sync allein aufgrund gespeicherter Daemon-Konfiguration.

Das Terminal hat die Rechte des Container-Benutzers. Git-Hooks, Aliase, Editoren und native Git-Durchreichung können Programme im Container ausführen und beschreibbare Mounts ändern. Es ist eine Verwaltungsoberfläche für vertrauenswürdige Administratoren. Ändernde Aktionen benötigen eine ausdrückliche Bestätigung in der Oberfläche; eine reine Shell-Sitzung wird nicht gestartet.

~~~sh
docker compose logs -f
docker compose pull
docker compose up -d --no-build
docker compose down
~~~

docker compose down erhält Repositories und Datenvolume. docker compose down -v würde auch den Token-Keyring entfernen.

## GitHub-Build und Prüfungen

Der Workflow .github/workflows/multigit-image.yml läuft bei Änderungen an MultiGit auf main, bei Tags multigit-v*, bei Pull Requests und manuell über **Actions → Build MultiGit image → Run workflow**.

Er führt Python-Tests einschließlich Linux-PTY-Integration aus, baut zunächst ein AMD64-Testimage und prüft dessen Start, Keyring, Neustart, Repository-API und echte native Terminalbefehle. Erst danach folgen AMD64-/ARM64-Build und Veröffentlichung auf GHCR. Pull Requests veröffentlichen kein Image. Der aktuelle Upstream-Commit wird vor jedem Build aufgelöst und im Image festgehalten.

Registry-Anmeldung erfolgt mit dem integrierten GITHUB_TOKEN. Tags: latest auf dem Standardbranch, sha-<Commit> und beispielsweise v1.0.0 bei einem Release-Tag multigit-v1.0.0. Private GHCR-Pakete erfordern docker login ghcr.io oder eine öffentlich gesetzte Paketsichtbarkeit.

Lokale Tests:

~~~sh
python -m unittest discover -s multigit -p 'test_*.py' -v
~~~

Die Terminal-Bibliothek xterm.js 5.5.0 wird lokal mitgeliefert; Lizenz und Herkunft stehen unter assets/. Die Oberfläche lädt keine Terminal-Skripte von einem CDN.

## Automatischer Versionsbericht

Nach erfolgreicher Veröffentlichung liest der Workflow beide Images über ihre Plattform-Digests aus und erzeugt multigit/versions.yaml. Er dokumentiert pro Architektur MultiGit samt Upstream-Commit, Python, Git, OpenSSH, OpenSSL, SQLite, Keyring, D-Bus, GnuPG, Nano, alle Debian-Pakete und vorhandene Python-Distributionen. Zusätzlich werden Rust-/Cargo-Versionen aus der Build-Stufe, die in Cargo.lock erfassten Pakete sowie xterm.js-Version und Asset-Prüfsumme festgehalten. Das Lockfile enthält auch optionale, Entwicklungs- und plattformfremde Pakete; es ist kein Nachweis ausschließlich einkompilierter Rust-Pakete.

Der Bericht enthält den eigenen Quell-Commit, Index-Digest, Plattform-Digests, Image-Erstellungszeitpunkte und Messdatum. Die Messung läuft ohne Netzwerkzugriff innerhalb der Container und umgeht den Web-/Keyring-Start.

Auf dem unveränderten Standardbranch wird nur multigit/versions.yaml automatisch committet. Bei Tags oder wenn der Branch während des Builds weitergelaufen ist, bleibt der Bericht als GitHub-Artefakt verfügbar. Pull Requests schreiben nichts zurück. Der Versionsbericht ist vom Build-Pfadfilter ausgeschlossen, um Wiederholungsschleifen zu vermeiden. Bis zum ersten erfolgreichen Build enthält die Datei einen ausdrücklich gekennzeichneten Platzhalter.
