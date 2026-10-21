FROM rust:bookworm AS builder
ARG MULTIGIT_REF=master
RUN apt-get update && apt-get install -y --no-install-recommends git pkg-config cmake libssl-dev libdbus-1-dev libssh2-1-dev zlib1g-dev libkeyutils-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone https://github.com/TIVerse/multigit.git . && git checkout "$MULTIGIT_REF" && cargo build --locked --release --bin multigit && git rev-parse HEAD > /src/upstream-revision

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 git nano gnupg pinentry-curses dbus gnome-keyring libsecret-tools openssh-client ca-certificates libssl3 libdbus-1-3 libssh2-1 zlib1g libkeyutils1 && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home app && mkdir /repos /data && chown app:app /repos /data
COPY --from=builder /src/target/release/multigit /usr/local/bin/multigit
RUN ln -s /usr/local/bin/multigit /usr/local/bin/mg
COPY web.py settings.py catalog.py overview.py terminal.py pty_worker.py entrypoint.py index.html app.js /app/
COPY assets /app/assets
COPY --from=builder /src/upstream-revision /app/upstream-revision
ENV HOME=/data XDG_CONFIG_HOME=/data/config PYTHONUNBUFFERED=1 GIT_TERMINAL_PROMPT=0
USER app
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["dbus-run-session", "--", "python3", "entrypoint.py"]
