# GitMesh

Eigener Sync-Manager für GitHub, GitLab und Forgejo/Gitea mit deutscher Weboberfläche. Python, SQLite und Git CLI; `cryptography` schützt gespeicherte Provider-Tokens. Zwei Docker-Dienste teilen das Datenvolume: Webserver und ein unabhaengiger Worker-Supervisor mit eigenen Job-Prozessen.

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
7. Unter **Discovery** Repositories suchen oder unter **Sync-Profile** manuell Profile anlegen. Ziel-Repositories müssen existieren oder im Profil die automatische Anlage aktivieren. Discovery-Importe sind zunächst deaktivierte Entwürfe.
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

SSH bleibt möglich: Schlüssel, Konfiguration und geprüfte `known_hosts` unter `ssh/` bereitstellen und den auskommentierten Mount aktivieren. UID 10001 benötigt Leserechte. Alternativ koennen private Dienstschluessel und verifizierte known_hosts unter Provider im Webinterface hinterlegt werden. Bei einem Mount diesen in beiden Docker-Diensten aktivieren. SCP-Kurzformen wie `git@host:org/repo.git` durch eine vollständige URL wie `ssh://git@host/org/repo.git` ersetzen.

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

Ein unabhaengiger Worker-Supervisor verarbeitet die dauerhafte SQLite-Warteschlange. Jede Worker-Gruppe startet bis zu acht parallele Job-Prozesse. Pro Profil gibt es höchstens einen wartenden oder laufenden Auftrag. Jedes Profil hat ein Intervall von 30 bis 86400 Sekunden, **0** bedeutet nur manuell/Webhook; ein leeres Feld übernimmt das Standardintervall. Neue oder geänderte Zeitpläne starten mit einem Versatz von höchstens 29 Sekunden.

**Pause** und **Stopp** verhindern neue Job-Starts und neue geplante Aufträge; aktive Jobs dürfen fertiglaufen. Bereits wartende und manuell eingereihte Jobs bleiben erhalten. **Starten/Fortsetzen** verarbeitet sie wieder. Festplatten-, RAM- und Systemlast-Grenzen halten Starts bei Ressourcenknappheit zurück. RAM-/Lastwerte sind nur verfügbar, wenn das Betriebssystem sie bereitstellt; auf Linux sind dies Hostmetriken.

Abruf- und andere wiederholbare Fehler werden bis zu dreimal mit Backoff versucht. Docker-Job-Prozesse wiederholen auch erkennbare voruebergehende Push-Netzwerkfehler nach erneutem Einlesen. Branchschutz, abgelehnte Updates und veraltete Leases werden nicht automatisch erneut versucht.

Beim Dienstneustart bleiben wartende Jobs erhalten. Unterbrochene laufende Jobs erhalten den Status `error` und können erneut eingereiht werden. Weboberflaeche und Worker sind getrennte Docker-Dienste. Ein Neustart ausschliesslich des Webdiensts laesst laufende Jobs weiterarbeiten. Ein Neustart des Worker-Diensts betrifft seine aktiven Jobs. Die Compose-Dateien gewähren beim Stoppen fünf Minuten für den Abschluss, danach darf Docker den Prozess beenden. Betriebssystem-Sperren verhindern doppelte Web-/Worker-Supervisoren und parallele Zugriffe auf dieselbe Repository-Zuordnung. Mehrere unabhängige Instanzen benötigen getrennte Volumes.

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

Git-Tests verwenden ausschließlich temporäre lokale Repositories. Tests prüfen unter anderem Divergenzen, Branch-Filter, Tags, Force-with-lease-Rennen, verschlüsselte Tokens, Discovery-Antworten, Rollentrennung, Sitzungen, Parallelität, Queue-Persistenz und Ressourcenlimits. Der Browser-Test startet einen isolierten lokalen Dienst und prüft Formulare, Anmeldung und mobile Darstellung. Discovery und Repository-Werkzeuge verwenden im Browser-Test Fixtures; er greift nicht auf echte Provider-Konten zu.

Optional: `GITMESH_TEST_GIT` setzt die Git-Binärdatei für Tests; `GITMESH_PLAYWRIGHT` eine externe Playwright-Installation; `GITMESH_BROWSER_EXECUTABLE` einen vorhandenen Browser und `GITMESH_SCREENSHOT` einen Screenshot-Ausgabepfad. `GITMESH_HOST` und `GITMESH_PORT` überschreiben die Server-Bindung, standardmäßig `0.0.0.0:8080`.

## Grenzen

Keine automatische Anlage von Organisationen/Gruppen, keine OAuth-/SSO-Anmeldung und keine ueber mehrere Hosts verteilten Worker. Keine Issues, Pull Requests, Releases, Wikis, Git Notes, speziellen Forge-Refs oder Git-LFS-Objekte. Submodule übertragen nur Git-Verweise; ihre Repositories benötigen eigene Profile. Einweg-Sync ist keine vollständige Mirror-Löschung.


## Fehlende Ziel-Repositories automatisch anlegen

Unter **Sync-Profile** pro Profil **Fehlende Ziel-Repositories automatisch anlegen** aktivieren und speichern. Den **Quell-/Start-Remote** festlegen und jedem anderen Remote einen gespeicherten Provider mit API-Token zuordnen. Die Ziel-URL bestimmt Besitzer bzw. Namespace und Repository-Namen. Standard ist **privat**; die Sichtbarkeit kann vor dem Anlegen explizit auf oeffentlich umgestellt werden.

GitMesh liest zuerst die Quelle erfolgreich ein, prueft dann die Ziele per API und legt ausschliesslich bei HTTP 404 ein leeres Repository an. Keine README, Lizenz oder .gitignore und kein fremder Start-Commit. Bestehende Repositories werden weder neu angelegt noch in ihrer Sichtbarkeit veraendert. Die Quelle wird nie automatisch angelegt. Angelegte Ziele erscheinen im Job-Feld `created_repositories`; bei spaeteren Fehlern bleiben diese Repositories erhalten.

Unterstuetzt werden Forgejo/Gitea und GitHub (eigenes Konto oder bestehende Organisation) sowie GitLab (bestehender Benutzer-/Gruppen-Namespace, auch Untergruppen). Fuer fremde persoenliche Konten werden keine administrativen Create-Endpunkte verwendet. Organisationen, Gruppen und Untergruppen muessen bereits existieren. Das API-Token braucht zusaetzlich zum Git-Push die Berechtigung zum Anlegen von Repositories im Ziel-Namespace. Bei 401/403 oder Netzwerkfehlern wird kein Create-Versuch ausgeloest. Gleichzeitige Anlage durch andere Prozesse wird durch erneutes Nachsehen abgefangen. Wiederholte Laeufe veraendern vorhandene Repositories nicht.

Die Option ist auch im bidirektionalen Modus moeglich: Der ausgewaehlte Start-Remote wird zuerst gelesen, alle anderen Remotes gelten fuer die Anlage als Ziele. Ohne aktivierte Option bleibt das bisherige Verhalten unveraendert.


## Repository-Werkzeuge im Webinterface

Unter **Repository-Werkzeuge** ein gespeichertes Profil auswaehlen. **Sync-Vorschau** liest alle erreichbaren Remotes neu ein und zeigt geplante Updates, maximal 30 Commits je Update und Konflikte. Sie legt keine Remote-Repositories an und pusht nichts. Fehlende, automatisch anzulegende Ziele werden markiert. Vorschauen gelten fuer den eingelesenen Stand; der echte Sync liest erneut ein. Die Anzeige begrenzt grosse Plaene auf 200 Updates.

**Branches und Tags laden** zeigt bis zu 1000 Referenzen pro Remote. Administratoren koennen Branches und leichtgewichtige Tags aus einer vorhandenen Referenz anlegen und einzelne Referenzen nach Bestaetigung loeschen. Lease-Pruefungen verhindern das Ueberschreiben zwischenzeitlicher Aenderungen. Ein spaeterer Sync kann geloeschte Referenzen aus anderen Remotes wiederherstellen; es gibt weiterhin keine automatische Loeschweitergabe.

**Historien vergleichen** zeigt beidseitig exklusive Commits, Diff-Statistik und einen auf 4000 Zeichen begrenzten Patch. Nach dem Vergleich kann ein Administrator einen Branch auf eines der beiden Remotes zusammenfuehren. Bei divergierenden Historien erzeugt GitMesh einen lokalen Merge-Commit; nur ein konfliktfreier Merge wird gepusht. Inhaltskonflikte liefern betroffene Dateien fuer die lokale Aufloesung. Historien ohne gemeinsamen Vorfahren werden nicht zusammengefuehrt. Die beiden Ausgangsstaende und das Ziel werden vor dem Schreiben geprueft. Weitere Remotes koennen danach per Sync aktualisiert werden. Manuelle Repository-Operationen und Worker-Syncs laufen nicht gleichzeitig.

## Provider-Webhooks registrieren

Unter **Repository-Werkzeuge** die oeffentlich erreichbare HTTPS-Adresse von GitMesh speichern, z. B. `https://gitmesh.example.org`. Jedes Remote des ausgewaehlten Profils benoetigt eine Provider-Zuordnung mit API-Token. Die im Profil benannte Secret-Umgebungsvariable muss im Container gesetzt sein. Anschliessend **Webhooks fuer Profil registrieren / aktualisieren** ausfuehren. GitHub benoetigt Webhook-Schreibrechte, GitLab und Forgejo/Gitea die entsprechende Projekt-/Repository-Verwaltungsberechtigung.

GitMesh registriert Push-/Tag-Ereignisse bei GitHub, GitLab und Forgejo/Gitea mit aktivierter TLS-Pruefung. Bestehende Hooks mit genau derselben Callback-Adresse werden aktualisiert, fremde Hooks bleiben erhalten. Die manuelle Aktualisierung uebernimmt auch ein geaendertes Secret. Unter **Sync-Profile** kann die automatische Registrierung beim Sync aktiviert werden: Fehlende Hooks werden angelegt, vorhandene Hooks bleiben unveraendert. Nach Secret-Wechsel die manuelle Aktualisierung ausfuehren. Bei mehreren Providern sind Teilerfolge moeglich; erneutes Ausfuehren gleicht fehlende Hooks ab. Secrets erscheinen nicht in den Ergebnissen. Branch-/Tag-Aktionen, Merge-Ergebnisse und manuelle Hook-Registrierungen erscheinen in der Job-Historie.


## Docker-Dienste und Worker-Gruppen

Beide Compose-Dateien starten `gitmesh` (Web/API) und `gitmesh-worker` (Supervisor/Scheduler). Beide nutzen dasselbe lokale Volume und denselben Tresorschluessel. Der Worker veroeffentlicht keinen Port. `docker compose restart gitmesh` startet nur die Oberflaeche neu; aktive Job-Prozesse im Worker bleiben erhalten. `docker compose stop gitmesh-worker` beendet die Annahme neuer Jobs und wartet auf laufende Prozesse. Nach fuenf Minuten darf Docker die Beendigung erzwingen; unterbrochene Jobs werden beim naechsten Start als Fehler markiert. Wartende Jobs bleiben erhalten. Der Heartbeat im Webinterface zeigt einen ausgefallenen Worker an.

Unter **Profile & Gruppen** benannte Worker-Gruppen anlegen, Parallelitaet und CPU-/RAM-/Plattenlimits setzen. Gruppen koennen unabhaengig gestartet, pausiert, fortgesetzt und gestoppt werden. Die globalen Worker-Schalter gelten zusaetzlich fuer alle Gruppen. Pausieren oder Stoppen laesst aktive Jobs fertiglaufen und behaelt wartende Auftraege. Pro Repository-Zuordnung wird ein eigener Prozess gestartet. Freigegebene Betriebssystem-Sperren erlauben parallele Jobs unter Linux, exklusive Sperren schuetzen manuelle Operationen und Konfigurationswechsel. Beim Prozessabbruch gibt das Betriebssystem Sperren automatisch frei. SQLite und Prozesssperren setzen ein lokales Docker-Volume auf demselben Host voraus, kein NFS-/Multi-Host-Cluster.

`GITMESH_ROLE=web` startet nur HTTP/API, `worker` nur den Supervisor. Der direkte Aufruf ohne Rolle startet zur Entwicklung Web und einen separaten Supervisor-Prozess gemeinsam. Fuer Docker die mitgelieferten Compose-Dateien mit getrennten Rollen verwenden. Web- und Worker-Dienst stets aus derselben Image-Version starten.

## Mehrere Repository-Zuordnungen je Profil

Unter **Profile & Gruppen** ein gemeinsames Profil anlegen. Es legt Richtung, Quelle, Konfliktregel, Branch-Filter, Tags, Intervall und Worker-Gruppe fest. Unter **Sync-Profile** jeder Repository-Zuordnung dieses gemeinsame Profil zuweisen. Die gemeinsamen Regeln sind verbindlich; die entsprechenden Felder werden im Zuordnungseditor gesperrt. Remote-URLs, Provider-Zuordnungen und Zuordnungsname bleiben individuell. Bei Einweg-Profilen muss derselbe Quell-Remote-Name in jeder Zuordnung vorkommen. Der Profil-Schalter reiht alle aktiven zugehoerigen Zuordnungen ein. Einzelne Zuordnungen behalten eigene Jobs, Cache und Protokolle. Vor dem Loeschen eines gemeinsamen Profils oder einer Gruppe die Zuordnungen loesen.

Bestehende Konfigurationen ohne `profiles` und `worker_sets` bleiben gueltig und verwenden eigene Regeln sowie die Gruppe `default`. Gemeinsame Profile werden als Objekt unter `profiles`, Worker-Gruppen unter `worker_sets` gespeichert; eine Zuordnung verweist mit `profile` und/oder `worker_set` darauf. Gruppen-Einstellungen erben nicht angegebene Ressourcenlimits aus `worker`.

## Transport und SSH im Webinterface

Unter **Provider → Transport und SSH** Provider auswaehlen und gespeicherten Transport laden. `auto` behaelt die Remote-URL bei, `https`, `http` oder `ssh` wandelt Git-Verbindungen fuer diesen Provider um; HTTP benoetigt die ausdrueckliche Provider-Freigabe. API-Zugriffe fuer Discovery, Repository-Anlage und Webhooks verwenden die konfigurierte Provider-Adresse: standardmaessig HTTPS, internes HTTP nur mit ausdruecklicher Freigabe. Fuer SSH koennen Host, Benutzer und Port angegeben werden; Standard sind Provider-Host, `git` und Port 22.

Ein eigener privater Dienstschluessel ohne Passphrase kann eingefuegt werden. GitMesh prueft ihn mit `ssh-keygen`, speichert ihn mit dem Tresorschluessel verschluesselt und gibt ihn nie ueber die API zurueck. Leere Eingabe behaelt einen vorhandenen Schluessel; zum Entfernen das entsprechende Feld aktivieren. Bei Hostwechsel muss ein gespeicherter Schluessel neu eingegeben oder entfernt werden. Verifizierte known_hosts-Eintraege sind bei gespeicherten Schluesseln erforderlich; SHA256-Fingerprints werden angezeigt. Hostschluessel unabhaengig pruefen. GitMesh akzeptiert keine unbekannten Hosts automatisch. Git-Operationen erhalten temporaere Dateien mit eingeschraenkten Rechten; diese werden danach geloescht. Passphrase-Schluessel und ein SSH-Agent im Webinterface werden nicht unterstuetzt. Ohne gespeicherten Schluessel bleibt die bestehende SSH-Mount-Konfiguration nutzbar.

## Wiederholungen bei Push-Netzwerkfehlern

Die Docker-Job-Prozesse wiederholen voruebergehende Fetch-/Provider-Fehler und erkennbare Push-Netzwerkfehler maximal dreimal mit exponentieller Wartezeit. Jede Wiederholung liest die Remotes vollstaendig neu ein und berechnet den Plan und gegebenenfalls die Leases neu; bereits erfolgreiche Teil-Updates werden dadurch erkannt. Authentifizierungsfehler, Branchschutz, abgelehnte Updates und veraltete Leases werden nicht blind erneut gepusht. Wiederholungen und Versuche stehen im Job-Protokoll. Die Konfliktregel bleibt bei jedem Versuch verbindlich.


## Docker-Integrationstest

Nach `docker build -t gitmesh:codex-test gitmesh` startet `python gitmesh/tests/docker-smoke.py` einen isolierten Compose-Verbund mit zufaelligem Projektname, lokalem Testport und eigenem Volume. Er prueft den ausfuehrbaren HTTPS-Helfer, beide Healthchecks, gemeinsame Profile, Gruppensteuerung, Konfigurationspersistenz und den unabhaengigen Web-Neustart. Anschliessend werden ausschliesslich die erzeugten Testdienste und das Testvolume entfernt. Es werden keine echten Provider kontaktiert. `GITMESH_TEST_IMAGE` kann ein anderes lokal verfuegbares Testimage auswaehlen. Der GitHub-Workflow fuehrt diesen Test vor dem Veroeffentlichen des Images aus.


## Dokumentierte Image-Versionen

Der GitHub-Workflow ermittelt nach erfolgreichem Veroeffentlichen die tatsaechlich installierten Versionen direkt aus dem Image mit seinem unveraenderlichen Digest. `gitmesh/versions.yaml` enthaelt Builddatum, Quell-Commit, Image-Digest sowie getrennt fuer AMD64 und ARM64 Python, Git, OpenSSH, OpenSSL, SQLite, Debian-Paketversionen und Python-Abhaengigkeiten. Die Versionen stammen aus dem fertigen Image, nicht aus dem GitHub-Runner. Die Abfrage loest den gemeinsamen Image-Index in eigene AMD64-/ARM64-Manifest-Digests auf und dokumentiert diese ebenfalls. Dadurch werden Konflikte im klassischen Docker-Image-Store des GitHub-Runners vermieden. Bis zum ersten erfolgreichen Lauf ist die Datei als ausstehend markiert.

Auf dem Standardbranch wird die Datei durch `github-actions[bot]` mit `[skip ci]` zurueckgeschrieben; reine Aenderungen dieser Datei loesen keinen neuen Image-Build aus. Hat sich der Branch waehrend des Builds weiterbewegt, wird der veraltete Bericht nicht daruebergeschrieben. Jeder Veroeffentlichungslauf stellt den Bericht zusaetzlich als Workflow-Artefakt bereit. Tag-Builds veraendern den Standardbranch nicht. Der Versionsjob benoetigt Schreibrechte fuer Repository-Inhalte; Branchschutz muss Bot-Commits erlauben, andernfalls bleibt das hochgeladene Artefakt verfuegbar.


## Interne Forgejo-/GitLab-Instanzen per HTTP

Im Providerformular **Internes HTTP erlauben** aktivieren und die direkte Basisadresse speichern, beispielsweise `http://172.30.10.20:40501`. Ein vorhandenes Token bei einem Host-/Protokollwechsel erneut eingeben. Die Option ist pro Provider gespeichert und standardmaessig deaktiviert. HTTP uebertraegt Tokens und Git-Daten unverschluesselt; nur in einem vertrauenswuerdigen internen Netz einsetzen.

Im Repository-Netz fuer `target` die direkte Git-URL `http://172.30.10.20:40501/BESITZER/REPOSITORY.git` eintragen und diesem Remote den internen Provider zuordnen. Unter Transport `auto` oder `Internes HTTP` auswaehlen. API und Git pruefen Protokoll, Host und Port; ein HTTPS-Provider gibt sein Token nicht an einen HTTP-Remote weiter. Automatische Repository-Anlage funktioniert ebenfalls mit dieser Freigabe. Die Docker-Dienste muessen die interne Adresse erreichen koennen. Liefert Forgejo bei Discovery weiterhin oeffentliche Clone-URLs aus seiner ROOT_URL, die interne Git-URL manuell im Netz eintragen.
