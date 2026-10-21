# GitMesh

Eigener Sync-Manager für GitHub, GitLab und Forgejo/Gitea mit deutscher Weboberfläche. Python, SQLite und Git CLI; `cryptography` schützt gespeicherte Provider-Tokens. Ein Container enthält Webserver, Scheduler und Worker-Pool.

## Start und Ersteinrichtung

1. `.env.example` nach `.env` kopieren. Zwei unabhängige zufällige Secrets mit mindestens 32 Zeichen für `GITMESH_ADMIN_TOKEN` und `GITMESH_WEBHOOK_SECRET` setzen.
2. Lokal bauen: `docker compose up -d --build` im Verzeichnis `gitmesh`.
3. Alternativ das veröffentlichte Image verwenden:
   ```sh
   docker compose -f docker-compose.ghcr.yml pull
   docker compose -f docker-compose.ghcr.yml up -d
   ```
   Diese Datei benötigt keinen lokalen Build und eignet sich für Portainer. Das Image muss zuvor vom GitHub-Workflow veröffentlicht worden sein.
4. [Webinterface öffnen](http://localhost:8088). Unter **Ersteinrichtung mit Admin-Token** verbinden.
5. Unter **Benutzer** einen persönlichen Administrator mit einem Passwort von mindestens 12 Zeichen anlegen. Anschließend abmelden und regulär anmelden. Der Bootstrap-Token bleibt als Administratorzugang für die Wiederherstellung verfügbar.
6. Unter **Provider** Instanzen und Zugangsdaten anlegen, danach **Verbindung testen**.
7. Unter **Discovery** Repositories suchen oder unter **Sync-Profile** manuell Profile anlegen. Ziel-Repositories müssen bereits existieren. Discovery-Importe sind zunächst deaktivierte Entwürfe.
8. Remotes zuordnen, Einstellungen prüfen, Profil aktivieren und speichern. Automatische Läufe beginnen mit kurzem Startversatz. Mit Intervall **0** läuft ein Profil nur manuell oder per Webhook.

Die Standardbindung ist ausschließlich localhost. Für externen Zugriff HTTPS-Reverse-Proxy mit Request-, Verbindungs- und Zeitlimits verwenden. Keine TLS-Prüfung wird abgeschaltet.

## Funktionen im Webinterface

| Bereich | Funktionen |
| --- | --- |
| Übersicht | Profile, aktive Jobs, Warteschlange, letzte Aktivitäten, manuelles Einreihen |
| Provider | Mehrere GitHub-/GitLab-/Forgejo-Instanzen; Tokens verwalten, Verbindung und Konto prüfen |
| Discovery | Eigenes Konto, Organisation/GitLab-Gruppe oder Benutzer; paginierte Auswahl und Profilentwürfe |
| Sync-Profile | Mehrere Remotes, Einweg- oder bidirektionaler Sync, Quelle, Branch-Globs, Tags, eigener Zeitplan |
| Konfliktregeln | Fast-forward, gesamtes Profil ablehnen oder explizites Einweg-Update mit Force-with-lease |
| Jobs & Protokolle | Letzte 100 Jobs mit Status, Auslöser, Versuchen, Änderungen und Konflikten; wartende Jobs abbrechen |
| Worker & System | Starten, pausieren, fortsetzen, stoppen; Parallelität 1–8 und Ressourcenlimits |
| Benutzer | Administratoren und Lesebenutzer; Passwort setzen, Konten deaktivieren, Sitzungen widerrufen |

Lesebenutzer können Daten einsehen und die Discovery nutzen. Alle Verwaltungsänderungen, Provider-Tests und Sync-Auslöser erfordern serverseitig Administratorrechte. Das bloße Verbergen von Schaltflächen ist nicht die Zugriffskontrolle.

## Provider und Zugangsdaten

Als URL die Website-Basis angeben, zum Beispiel `https://github.com`, `https://gitlab.example.org` oder `https://forgejo.example.org`. GitHub Enterprise verwendet `/api/v3`, GitLab `/api/v4`, Forgejo `/api/v1`. GitHub.com wird über `api.github.com` angesprochen. API-Antworten werden begrenzt, Requests haben Zeitlimits und folgen keinen Weiterleitungen mit Zugangsdaten.

Tokens benötigen passende API-Leserechte für Discovery und Repository-Schreibrechte für HTTPS-Pushes. Auf die vorgesehenen Repositories beschränken. Der Verbindungstest liest das Benutzerkonto und ergänzt einen noch leeren Git-Benutzernamen. Git-URLs enthalten keine Tokens. Beim HTTPS-Zugriff übergibt ein Askpass-Helfer das zugeordnete Token ausschließlich an den Git-Unterprozess; Weiterleitungen und externe Credential-Helper sind deaktiviert.

Ein leeres Token-Feld beim Bearbeiten behält den bisherigen Wert. **Gespeichertes Token entfernen** löscht es ausdrücklich. Beim Wechsel von Host oder Typ muss das Token neu eingegeben oder entfernt werden. Ein Provider kann erst entfernt werden, wenn kein gespeichertes Profil ihn mehr verwendet. Bei Änderungen mit aktiven Syncs zunächst deren Abschluss abwarten.

SSH bleibt möglich: Schlüssel, Konfiguration und geprüfte `known_hosts` unter `ssh/` bereitstellen und den auskommentierten Mount aktivieren. UID 10001 benötigt Leserechte. Private Schlüssel und Hostvertrauen werden über diesen Mount verwaltet. SCP-Kurzformen wie `git@host:org/repo.git` durch eine vollständige URL wie `ssh://git@host/org/repo.git` ersetzen.

API-Dokumentation: [GitHub](https://docs.github.com/en/rest/repos/repos), [GitLab](https://docs.gitlab.com/api/projects/), [Forgejo](https://forgejo.org/docs/latest/user/api/usage/).

## Sync-Verhalten

GitMesh liest zuerst alle Remotes. Im bidirektionalen Modus gewinnt eine vorhandene Branch-Spitze nur, wenn sie alle anderen Spitzen als Vorfahren enthält. Gleichstände erzeugen keine Pushes. Eigene Webhook-Rückmeldungen führen deshalb nicht zu endlosen Push-Schleifen.

Im Einwegmodus ist ausschließlich der gewählte Quell-Remote maßgeblich. Zusätzliche Branches auf Zielen werden nicht zurück importiert. Branch-Filter sind durch Komma getrennte Globs, etwa `main,release/*`; leer bedeutet alle Branches. Tags werden unabhängig davon über ihre Checkbox ausgewählt.

- **ff-only:** Nur Fast-forward-Updates bzw. fehlende Referenzen verteilen. Divergierende Branches und abweichende Tags als Konflikt melden; andere Referenzen können weiterlaufen.
- **reject:** Bei einem erkannten Konflikt vor dem ersten Push das gesamte Profil überspringen.
- **force-with-lease:** Nur im Einwegmodus und mit expliziter Bestätigung. Zielhistorie darf durch den Quellstand ersetzt werden, sofern der Zielstand seit dem Fetch unverändert ist. Gleichzeitige Zieländerungen führen zum Abbruch. Es gibt keinen unbedingten `--force`-Push.

Kein Modus propagiert Löschungen. Ein nur auf einem Remote gelöschter Branch kann aus anderen wiederhergestellt werden. Zum vollständigen Entfernen das Profil deaktivieren und die Referenz auf allen Remotes löschen. Branchschutz und Serverberechtigungen bleiben wirksam. Remote-Pushes über mehrere Server sind nicht atomar; nach einem Fehler können einzelne Ziele bereits aktualisiert sein.

Konflikte lokal zusammenführen und das Merge-Ergebnis pushen; der nächste Lauf verteilt es. Canonicalisierte Repository-URLs dürfen nicht mehrfach in Profilen vorkommen, auch nicht in umgekehrter Richtung. Unterschiedliche DNS-Aliase desselben Git-Servers lassen sich dabei nicht zuverlässig erkennen.

## Worker, Zeitpläne und Neustarts

Ein Supervisor verarbeitet eine dauerhafte SQLite-Warteschlange mit bis zu acht parallelen Jobs. Pro Profil gibt es höchstens einen wartenden oder laufenden Auftrag. Jedes Profil hat ein Intervall von 30 bis 86400 Sekunden, **0** bedeutet nur manuell/Webhook; ein leeres Feld übernimmt das Standardintervall. Neue oder geänderte Zeitpläne starten mit einem Versatz von höchstens 29 Sekunden.

**Pause** und **Stopp** verhindern neue Job-Starts und neue geplante Aufträge; aktive Jobs dürfen fertiglaufen. Bereits wartende und manuell eingereihte Jobs bleiben erhalten. **Starten/Fortsetzen** verarbeitet sie wieder. Festplatten-, RAM- und Systemlast-Grenzen halten Starts bei Ressourcenknappheit zurück. RAM-/Lastwerte sind nur verfügbar, wenn das Betriebssystem sie bereitstellt; auf Linux sind dies Hostmetriken.

Abruf- und andere wiederholbare Fehler werden bis zu dreimal mit Backoff versucht. Fehlgeschlagene Pushes werden innerhalb eines Jobs nicht automatisch wiederholt, damit eine abgelehnte Zielstand-Prüfung nicht sofort durch erneutes Einlesen umgangen wird. Ein später neu ausgelöster Job prüft den dann aktuellen Stand erneut.

Beim Dienstneustart bleiben wartende Jobs erhalten. Unterbrochene laufende Jobs erhalten den Status `error` und können erneut eingereiht werden. Die Weboberfläche und Worker laufen im selben Prozess; ein Container-Neustart unterbricht aktive Jobs. Die Compose-Dateien gewähren beim Stoppen fünf Minuten für den Abschluss, danach darf Docker den Prozess beenden. Eine Betriebssystem-Sperre verhindert zwei Prozesse auf demselben Datenverzeichnis. Mehrere unabhängige Instanzen benötigen getrennte Volumes.

## Sicherheit, Speicherung und Backups

- Einstellungen liegen atomar gespeichert in `/data/gitmesh.json`; Provider, Benutzer, Sitzungen, Jobs und Historie in `/data/history.db`.
- Provider-Tokens sind mit [Fernet](https://cryptography.io/en/stable/fernet/) authentifiziert verschlüsselt. Der automatisch erzeugte Schlüssel liegt mit eingeschränkten Dateirechten unter `/data/vault.key`. Optional einen gültigen Fernet-Schlüssel extern über `GITMESH_VAULT_KEY` bereitstellen.
- Datenbank und Schlüssel zusammen im selben Volume schützen nicht vor dem vollständigen Auslesen dieses Volumes. Backups und Hostzugriff entsprechend schützen. Ein fehlender vorhandener Schlüssel wird bei gespeicherten Tokens nicht stillschweigend ersetzt.
- Passwörter werden gesalzen mit PBKDF2-SHA256 und 600000 Iterationen gespeichert. Sitzungstokens sind zufällig, serverseitig nur gehasht gespeichert und laufen nach acht Stunden ab. Rollen-/Passwortänderungen und Deaktivierung widerrufen Sitzungen. Der letzte aktive persönliche Administrator kann nicht deaktiviert oder herabgestuft werden.
- Tokens bleiben im Browser-Arbeitsspeicher, nicht in Local Storage oder Cookies. Abmelden widerruft die Sitzung und leert sensible Formulare. Wiederholte fehlgeschlagene Passwortanmeldungen werden pro Adresse begrenzt.
- Der Container läuft ohne Root, mit schreibgeschütztem Root-Dateisystem und ohne zusätzliche Capabilities.

Für ein konsistentes Backup den Dienst stoppen und das gesamte Datenvolume sichern, einschließlich `vault.key`, Git-Objekten und SQLite-Dateien. Einen externen Tresorschlüssel und die Bootstrap-Konfiguration gesondert sichern. Bei Verlust des Schlüssels müssen Provider-Tokens neu eingerichtet werden. Historie und Caches werden derzeit nicht automatisch bereinigt.

Bestehende GitMesh-Konfigurationen bleiben gültig; fehlende neue Profilfelder erhalten sichere Standardwerte. Alte schreibgeschützte `/config/gitmesh.json`-Mounts entfernen und die Datei bei gestopptem Dienst als `/data/gitmesh.json` übernehmen. `GITMESH_CONFIG` unterstützt einen anderen beschreibbaren Konfigurationspfad. Die neuen Datenbanktabellen werden beim Start ergänzt.

## Webhooks

Push-Webhooks manuell auf `https://DEIN-HOST/hooks/github/PROFILNAME`, `/hooks/gitlab/PROFILNAME` oder `/hooks/forgejo/PROFILNAME` einrichten. Als Secret den Wert der im Profil angegebenen Umgebungsvariable verwenden, standardmäßig `GITMESH_WEBHOOK_SECRET`. GitHub und Forgejo werden mit HMAC-SHA256, GitLab mit dem Secret-Header geprüft. Payload-URLs werden nicht übernommen. Deaktivierte Profile werden auch durch Webhooks nicht gestartet.

## Automatischer GitHub-Build

`.github/workflows/gitmesh-image.yml` führt zuerst Python-Integrationstests und den Browser-Funktionstest aus. Anschließend baut er für `linux/amd64` und `linux/arm64` und veröffentlicht `ghcr.io/<owner>/gitmesh`.

- Änderungen an GitMesh auf `main`: `latest` und `sha-<vollständiger-commit>`.
- Tags wie `gitmesh-v1.0.0`: `v1.0.0` und Commit-Tag; `latest` bleibt unverändert.
- Montags 04:43 UTC und manuell über **Actions → Build GitMesh image → Run workflow**: erneuter Build mit aktuellen Basis-Images.
- Pull Requests: Tests und Build ohne Registry-Anmeldung und ohne Veröffentlichung.

Aktiv nach Commit und Push. Der Workflow verwendet `GITHUB_TOKEN` mit `packages: write`; kein weiteres Registry-Secret nötig. Actions und Package-Schreibzugriff müssen erlaubt sein. Neues GHCR-Package bei Bedarf auf **Public** stellen oder den Docker-Host anmelden. `GITMESH_IMAGE` wählt einen anderen Owner oder festen Tag. Laufende Container werden durch einen Build nicht automatisch aktualisiert; dafür `pull` und `up -d` ausführen.

## Entwicklung und Tests

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
npm ci --ignore-scripts
npx playwright install chromium
npm run test:browser
```

Git-Tests verwenden ausschließlich temporäre lokale Repositories. Tests prüfen unter anderem Divergenzen, Branch-Filter, Tags, Force-with-lease-Rennen, verschlüsselte Tokens, Discovery-Antworten, Rollentrennung, Sitzungen, Parallelität, Queue-Persistenz und Ressourcenlimits. Der Browser-Test startet einen isolierten lokalen Dienst und prüft Formulare, Anmeldung und mobile Darstellung. Nur dessen Discovery-Antwort ist eine Fixture; er greift nicht auf echte Provider-Konten zu.

Optional: `GITMESH_TEST_GIT` setzt die Git-Binärdatei für Tests; `GITMESH_PLAYWRIGHT` eine externe Playwright-Installation; `GITMESH_BROWSER_EXECUTABLE` einen vorhandenen Browser und `GITMESH_SCREENSHOT` einen Screenshot-Ausgabepfad. `GITMESH_HOST` und `GITMESH_PORT` überschreiben die Server-Bindung, standardmäßig `0.0.0.0:8080`.

## Grenzen

Keine automatische Anlage von Remote-Repositories oder Webhooks, keine OAuth-/SSO-Anmeldung, keine Worker-Gruppen oder unabhängigen Worker-Prozesse. Keine Issues, Pull Requests, Releases, Wikis, Git Notes, speziellen Forge-Refs oder Git-LFS-Objekte. Submodule übertragen nur Git-Verweise; ihre Repositories benötigen eigene Profile. Einweg-Sync ist keine vollständige Mirror-Löschung.
