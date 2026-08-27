ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ARG BUILD_DATE=unknown
ARG BASE_IMAGE_REF=python:3.12-slim-bookworm
ARG BASE_IMAGE_DIGEST=unknown

LABEL org.opencontainers.image.title="Mail Gateway"
LABEL org.opencontainers.image.description="POP3/IMAP Mail Gateway for Synology MailPlus"
LABEL org.opencontainers.image.authors="sandmaennche5"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/sandmaennchen5/docker-compose"
LABEL org.opencontainers.image.created="${BUILD_DATE}"
LABEL io.sandmaennchen.mail-gateway.base-image="${BASE_IMAGE_REF}"
LABEL io.sandmaennchen.mail-gateway.base-image-digest="${BASE_IMAGE_DIGEST}"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MSMTP_QUEUE=/var/spool/msmtp

RUN apt-get update && apt-get install -y --no-install-recommends \
    getmail6 \
    msmtp \
    msmtp-mta \
    supervisor \
    curl \
    procps \
    ca-certificates \
    tzdata \
    bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mail-gateway

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker-entrypoint.sh scripts/healthcheck.sh

COPY scripts/msmtpq /usr/local/bin/msmtpq
RUN chmod +x /usr/local/bin/msmtpq

RUN useradd -r -s /sbin/nologin -d /var/spool/msmtp -m mail 2>/dev/null \
    || usermod -d /var/spool/msmtp mail 2>/dev/null \
    || true

RUN mkdir -p \
    /config \
    /runtime/getmail \
    /runtime/status \
    /logs \
    /var/spool/msmtp \
    /var/log/supervisor && \
    chown -R mail:mail \
    /runtime \
    /logs \
    /var/spool/msmtp

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD /opt/mail-gateway/scripts/healthcheck.sh

ENTRYPOINT ["/opt/mail-gateway/docker-entrypoint.sh"]
