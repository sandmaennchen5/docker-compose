# Oscam ICAM EMU (Docker)

Dieses Verzeichnis enthält ein Oscam-Build mit:

- ICAM-Streamrelay
- EMU
- DVBAPI-Net
- WebIF

## Build

Der Build läuft automatisch über GitHub Actions:

- Patch: `patches/oscam_emu_icam_dvbapi.patch`
- Binary: `oscam`
- Image: `ghcr.io/sandmaennchen5/oscam-icam:latest`

## Docker Compose Beispiel

```yaml
services:
  oscam-icam:
    image: ghcr.io/sandmaennchen5/oscam-icam:latest
    container_name: oscam-icam
    restart: unless-stopped
    ports:
      - "8888:8888"
      - "9000:9000"
    volumes:
      - ./oscam-icam/config:/config
