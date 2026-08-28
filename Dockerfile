# syntax=docker/dockerfile:1.7

ARG RUST_VERSION=1.88

FROM --platform=$TARGETPLATFORM rust:${RUST_VERSION}-bookworm AS builder
WORKDIR /app

COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY config ./config
COPY README.md ./README.md
RUN cargo build --release --locked

FROM --platform=$TARGETPLATFORM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ncurses-base \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/target/release/tbt-monitor-tui /usr/local/bin/tbt-monitor-tui
COPY config/monitor.cfg /app/config/monitor.cfg
RUN mkdir -p /out
VOLUME ["/out"]

ENV TERM=xterm-256color
ENTRYPOINT ["tbt-monitor-tui"]
CMD ["monitor", "--config", "/app/config/monitor.cfg"]
