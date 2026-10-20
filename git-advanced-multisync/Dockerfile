# Build context: checked-out DEC-LLC/git-advanced-multisync source.
# Run the web application directly; it starts its workers itself.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git openssh-client procps tini \
        perl libmojolicious-perl libdbi-perl libdbd-pg-perl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /var/lib/gitmsyncd gitmsyncd \
    && mkdir -p /var/lib/gitmsyncd/workdir \
    && chown gitmsyncd:gitmsyncd /var/lib/gitmsyncd/workdir

WORKDIR /opt/gitmsyncd
COPY bin/ ./bin/
COPY lib/ ./lib/
COPY web/ ./web/
COPY db/ ./db/
COPY cpanfile LICENSE ./

# Fail the build on missing Perl dependencies or incompatible source code.
RUN perl -c bin/gitmsyncd.pl \
    && perl -c bin/gitmsyncd-worker.pl

ENV GITMSYNCD_LISTEN=http://0.0.0.0:9097 \
    GITMSYNCD_WORKDIR=/var/lib/gitmsyncd/workdir \
    GITMSYNCD_MAX_FORKS=4 \
    GITMSYNCD_MAX_LOAD=3.2 \
    GITMSYNCD_MIN_MEM_MB=256 \
    GITMSYNCD_MIN_DISK_MB=1024

USER gitmsyncd
EXPOSE 9097
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["perl", "/opt/gitmsyncd/bin/gitmsyncd.pl"]
