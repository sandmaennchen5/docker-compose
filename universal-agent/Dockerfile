# syntax=docker/dockerfile:1.7

ARG PORTAINER_IMAGE=portainer/agent:sts
ARG HAWSER_IMAGE=ghcr.io/finsys/hawser:latest
ARG PORTWING_IMAGE=ghcr.io/codeswhat/portwing:latest

FROM ${PORTAINER_IMAGE} AS portainer
FROM ${HAWSER_IMAGE} AS hawser
FROM ${PORTWING_IMAGE} AS portwing

FROM alpine:3.22

ARG PORTAINER_DIGEST=unknown
ARG HAWSER_DIGEST=unknown
ARG PORTWING_DIGEST=unknown
ARG PORTAINER_VERSION=unknown
ARG HAWSER_VERSION=unknown
ARG PORTWING_VERSION=unknown
ARG BUILD_DATE=unknown
ARG PORTAINER_AMD64_DIGEST=unknown
ARG PORTAINER_ARM64_DIGEST=unknown
ARG HAWSER_AMD64_DIGEST=unknown
ARG HAWSER_ARM64_DIGEST=unknown
ARG PORTWING_AMD64_DIGEST=unknown
ARG PORTWING_ARM64_DIGEST=unknown

LABEL org.opencontainers.image.title="Universal Docker Agent" \
      org.opencontainers.image.description="Portainer Agent + Dockhand Hawser + Drydock Portwing" \
      org.opencontainers.image.source="https://github.com/sandmaennchen5/docker-compose" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      io.sandmaennchen.universal-agent.portainer-version="${PORTAINER_VERSION}" \
      io.sandmaennchen.universal-agent.hawser-version="${HAWSER_VERSION}" \
      io.sandmaennchen.universal-agent.portwing-version="${PORTWING_VERSION}" \
      io.sandmaennchen.universal-agent.portainer-digest="${PORTAINER_DIGEST}" \
      io.sandmaennchen.universal-agent.hawser-digest="${HAWSER_DIGEST}" \
      io.sandmaennchen.universal-agent.portwing-digest="${PORTWING_DIGEST}" \
      io.sandmaennchen.universal-agent.portainer-amd64-digest="${PORTAINER_AMD64_DIGEST}" \
      io.sandmaennchen.universal-agent.portainer-arm64-digest="${PORTAINER_ARM64_DIGEST}" \
      io.sandmaennchen.universal-agent.hawser-amd64-digest="${HAWSER_AMD64_DIGEST}" \
      io.sandmaennchen.universal-agent.hawser-arm64-digest="${HAWSER_ARM64_DIGEST}" \
      io.sandmaennchen.universal-agent.portwing-amd64-digest="${PORTWING_AMD64_DIGEST}" \
      io.sandmaennchen.universal-agent.portwing-arm64-digest="${PORTWING_ARM64_DIGEST}"


RUN apk add --no-cache ca-certificates docker-cli docker-cli-compose tini procps \
    && mkdir -p /data/hawser/stacks /data/portwing/stacks /run/secrets

COPY --from=portainer /app/agent /usr/local/bin/portainer-agent
COPY --from=hawser /usr/local/bin/hawser /usr/local/bin/hawser
COPY --from=portwing /usr/bin/portwing /usr/local/bin/portwing
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/portainer-agent /usr/local/bin/hawser \
    /usr/local/bin/portwing /usr/local/bin/entrypoint.sh

ENV ENABLE_PORTAINER=true \
    ENABLE_HAWSER=true \
    ENABLE_PORTWING=true \
    HAWSER_PORT=2376 \
    PORTWING_PORT=3000 \
    HAWSER_STACKS_DIR=/data/hawser/stacks \
    PORTWING_STACKS_DIR=/data/portwing/stacks

EXPOSE 9001 2376 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD /usr/local/bin/entrypoint.sh health || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
