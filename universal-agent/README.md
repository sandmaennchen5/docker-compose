# Universal Docker Agent

## Große Image-Pulls: „Failed to up via Hawser: Connection error“

Die Meldung kann durch einen Timeout entstehen; ohne Logs ist die Ursache
nicht eindeutig. Hawser 0.2.50 unterstützt `COMPOSE_TIMEOUT` getrennt vom
normalen `REQUEST_TIMEOUT`. Der Universalagent reicht dafür folgende Werte
aus der `.env` an Hawser weiter (Sekunden):

```dotenv
HAWSER_REQUEST_TIMEOUT=30
HAWSER_COMPOSE_TIMEOUT=1800
```

Der Standard für Compose bleibt 900 Sekunden (15 Minuten); das Beispiel
erhöht ihn auf 30 Minuten. Nach einer Änderung muss der Container neu erstellt
werden. Die Weitergabe benötigt ein Image mit dem aktualisierten Entrypoint;
eine Änderung der `.env` allein aktualisiert das Image nicht.

Bei einer direkten Verbindung muss auch im **Dockhand-Container**
`COMPOSE_TIMEOUT=1800` gesetzt und dieser neu erstellt werden. Reverse Proxies
im Verbindungsweg müssen entsprechend lange Requests zulassen.

Im **Edge-Modus** begrenzt Dockhand im aktuell geprüften Quellcode die Anfrage
an Hawser separat auf 300 Sekunden. Ein höheres Agent-Timeout hebt diese
Grenze nicht auf. Als Workaround die Images auf dem Zielhost im Verzeichnis
des betroffenen Stacks vorab mit `docker compose pull` laden und anschließend
das Deployment in Dockhand erneut starten.

`HAWSER_SERVER_URL` wird an Hawsers `DOCKHAND_SERVER_URL` weitergereicht;
mit gesetztem Token aktiviert die WebSocket-URL den Edge-Modus.

Zur Diagnose auf dem Zielhost `docker logs --since 30m universal-agent`
und die Dockhand-Logs prüfen. Die Zeit bis zum Abbruch hilft, die wirksame
Timeout-Grenze einzugrenzen.

Quellen: [Hawser-Konfiguration 0.2.50](https://github.com/Finsys/hawser/blob/v0.2.50/internal/config/config.go),
[Timeout-Fehlerbericht](https://github.com/Finsys/hawser/issues/95),
[Dockhand-Verbindungslogik](https://github.com/Finsys/dockhand/blob/main/src/lib/server/docker.ts).

Die Versionsprüfung ist jetzt plattformgenau.

Für jeden Agent werden gespeichert:

- SemVer
- Multi-Arch Index-Digest
- `linux/amd64` Digest
- `linux/arm64` Digest
- verifizierte Version-Referenz
- `version_matches_digest`

Dadurch wird Portainer korrekt erkannt, auch wenn Docker Hub für `sts`,
`2.44.0`, Windows und Linux unterschiedliche Manifest-Digests zeigt.

Beispiel:

```yaml
agents:
  portainer:
    source_ref: "portainer/agent:sts"
    version: "2.44.0"
    version_ref: "portainer/agent:2.44.0"
    version_matches_digest: true
    index_digest: "sha256:..."
    platforms:
      linux/amd64: "sha256:..."
      linux/arm64: "sha256:..."
```

`universal-agent/CHANGELOG.md` zeigt bei jedem Build außerdem die aktuellen
amd64- und arm64-Digests aller drei Agents.


## Portainer STS SemVer detection

Portainer LTS and STS are maintained in parallel. Because of that,
GitHub `releases/latest` may point to the LTS release and is not reliable
for resolving `portainer/agent:sts`.

The workflow now scans Portainer release tags and verifies candidates against
the actual `linux/amd64` and `linux/arm64` digests of `portainer/agent:sts`.

Only a version whose platform digests match exactly is written to
`versions.yaml` and `CHANGELOG.md`.
