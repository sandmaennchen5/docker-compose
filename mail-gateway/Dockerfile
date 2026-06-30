FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Mail Gateway"
LABEL org.opencontainers.image.description="POP3/IMAP Mail Gateway for Synology MailPlus"
LABEL org.opencontainers.image.authors="sandmaennche5"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/sandmaennche5/docker-compose"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # msmtpq stores queued mail here
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

# Pre-create all runtime directories so volumes can be mounted without root issues
RUN mkdir -p \
    /config \
    /runtime/getmail \
    /runtime/status \
    /logs \
    /var/spool/msmtp \
    /var/log/supervisor

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD /opt/mail-gateway/scripts/healthcheck.sh

ENTRYPOINT ["/opt/mail-gateway/docker-entrypoint.sh"]