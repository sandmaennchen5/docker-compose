# Universal Docker Agent

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
