# docker-compose

Docker Images & Compose Stacks von **sandmaennche5**.  
Alle Images werden automatisch über GitHub Actions gebaut und auf [GHCR](https://ghcr.io/sandmaennche5/docker-compose) veröffentlicht.

---

## Images

| Image | Beschreibung | Basis |
|---|---|---|
| [code-server](./codeserver/) | Erweitertes Code-Server Image mit Docker, DevContainer CLI und persistenten Extensions | ![codeserver](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fgithub.com%2Fsandmaennchen5%2Fdocker-compose%2Fraw%2Frefs%2Fheads%2Fmain%2Fcodeserver%2Fversions.yaml&query=upstream.tag&label=lscr.io/linuxserver/code-server) |
| [mail-gateway](./mail-gateway/) | POP3/IMAP Mail-Gateway für Synology MailPlus | ![getmail6](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fgithub.com%2Fsandmaennchen5%2Fdocker-compose%2Fraw%2Frefs%2Fheads%2Fmain%2Fmail-gateway%2Fversions.yaml&query=packages.getmail6&label=getmail6) ![msmtp](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fgithub.com%2Fsandmaennchen5%2Fdocker-compose%2Fraw%2Frefs%2Fheads%2Fmain%2Fmail-gateway%2Fversions.yaml&query=packages.msmtp&label=msmtp) ![msmtp-mta](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fgithub.com%2Fsandmaennchen5%2Fdocker-compose%2Fraw%2Frefs%2Fheads%2Fmain%2Fmail-gateway%2Fversions.yaml&query=packages.msmtp-mta&label=msmtp-mta) |
| [universal-agent](./universal-agent/) | Agents für Portainer, Dockhand und Drydock | ![portainer](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fgithub.com%2Fsandmaennchen5%2Fdocker-compose%2Fraw%2Frefs%2Fheads%2Fmain%2Funiversal-agent%2Fversions.yaml&query=agents.portainer.version&label=portainer/agent:sts) ![dockhand](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fgithub.com%2Fsandmaennchen5%2Fdocker-compose%2Fraw%2Frefs%2Fheads%2Fmain%2Funiversal-agent%2Fversions.yaml&query=agents.hawser.version&label=ghcr.io/finsys/hawser) ![drydock](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fgithub.com%2Fsandmaennchen5%2Fdocker-compose%2Fraw%2Frefs%2Fheads%2Fmain%2Funiversal-agent%2Fversions.yaml&query=agents.hawser.version&label=ghcr.io/codeswhat/portwing) |

---
