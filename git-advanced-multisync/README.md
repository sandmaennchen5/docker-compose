# Git Advanced Multisync image

Der Workflow `.github/workflows/git-advanced-multisync-image.yml` baut den
Quellcode von https://github.com/DEC-LLC/git-advanced-multisync mit dessen
`Containerfile` und veröffentlicht das Image in der GitHub Container Registry.

## Erster Build

1. Den Workflow committen und nach GitHub pushen.
2. Unter **Actions → Build Git Advanced Multisync image → Run workflow** starten.
   Als `upstream_ref` kann ein Branch, Tag oder Commit des Upstream-Projekts
   angegeben werden. Für reproduzierbare Builds einen vollständigen Commit verwenden.
3. Das Image steht nach erfolgreichem Build unter
   `ghcr.io/<github-owner>/git-advanced-multisync:latest` bereit.
   Den Owner kleinschreiben. Zusätzlich wird `sha-<vollständiger-upstream-commit>`
   als fester Versions-Tag veröffentlicht.

Ein Push dieser Workflow-Datei nach `main` startet ebenfalls einen Build.
Zusätzlich prüft der Workflow täglich um 04:17 UTC den aktuellen Commit auf
Upstream-`main` (05:17 Uhr MEZ / 06:17 Uhr MESZ). Wenn dessen Image bereits als
`latest` veröffentlicht ist, wird der Build übersprungen. Andernfalls wird
automatisch gebaut und veröffentlicht. Bei Registry-Lesefehlern wird ebenfalls
ein Build versucht. Manuelle Starts und Workflow-Änderungen bauen immer neu.
Gebaut wird für `linux/amd64`. Der Zeitplan wird nach dem Push auf den
Standardbranch aktiv; GitHub kann geplante Starts verzögern.

Die Prüfung erkennt Quellcode-Updates. Aktualisierte Basis-Images oder
Abhängigkeiten ohne neuen Upstream-Commit lösen keinen Build aus; dafür den
Workflow manuell starten. Laufende Container werden nicht automatisch aktualisiert.

Der Workflow verwendet das vorhandene `GITHUB_TOKEN`; ein zusätzliches Secret
ist nicht erforderlich. Das Repository muss GitHub Actions und das Schreiben
von Packages erlauben. Ein neues GHCR-Package ist üblicherweise privat.
Für anonymes Herunterladen unter den Package-Einstellungen die Sichtbarkeit
auf **Public** stellen oder den Docker-Host bei GHCR anmelden.

## Verwendung mit Docker Compose / Portainer

In der Compose-Konfiguration des Projekts den bisherigen `build:`-Abschnitt
des Anwendungsdienstes entfernen und stattdessen eintragen:

```yaml
image: ghcr.io/<github-owner>/git-advanced-multisync:latest
```

Die übrige Konfiguration, insbesondere PostgreSQL, Datenbankinitialisierung,
Umgebungsvariablen und Volumes, wird weiterhin benötigt. Die offizielle Vorlage:
https://github.com/DEC-LLC/git-advanced-multisync/blob/main/packaging/container/docker-compose.example.yml

Anschließend auf dem Docker-Host ausführen:

```sh
docker compose pull
docker compose up -d
```

In Portainer denselben Image-Namen im Stack verwenden und beim Aktualisieren
das Image erneut herunterladen lassen. Für eine festgelegte Version den
`sha-…`-Tag statt `latest` verwenden.
