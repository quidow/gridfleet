FROM rust:1.96-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake make gcc g++ \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY router/Cargo.toml router/Cargo.lock ./
COPY router/src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /usr/sbin/nologin appuser
COPY --from=build /src/target/release/gridfleet-router /usr/local/bin/gridfleet-router
USER appuser
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
  CMD ["curl", "-fsS", "http://127.0.0.1:4444/healthz"]
ENTRYPOINT ["gridfleet-router"]
CMD ["--listen", "0.0.0.0:4444", "--backend", "http://backend:8000"]
