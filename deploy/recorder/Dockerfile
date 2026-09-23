FROM rust:1-slim@sha256:34fb2f168c432d421a09883c663b275b33cbb30f6b18642fbd09a684c6546d0e AS race-core
WORKDIR /build
COPY rust/race-core/Cargo.toml rust/race-core/Cargo.lock ./
COPY rust/race-core/src ./src
RUN cargo build --release --locked

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /opt/race-lens/backend

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the dependency graph before copying the full implementation.  The
# editable package remains rooted here, so the later COPY supplies the complete
# module without a second build/install pass.
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/racelens/__init__.py ./racelens/__init__.py
RUN pip install --no-cache-dir -e ".[recorder,storage]"
COPY backend/racelens/ ./racelens/
ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision=$VCS_REF
COPY --from=race-core /build/target/release/race-core /usr/local/bin/race-core
COPY deploy/recorder/run.sh /usr/local/bin/race-lens-recorder
RUN chmod 0555 /usr/local/bin/race-lens-recorder /usr/local/bin/race-core \
    && mkdir -p /var/lib/race-lens-recorder/data \
        /var/lib/race-lens-recorder/state \
        /var/lib/race-lens-recorder/raw

ENV HOME=/var/lib/race-lens-recorder/state \
    HF_HOME=/var/lib/race-lens-recorder/data/models \
    PYTHONUNBUFFERED=1

WORKDIR /var/lib/race-lens-recorder
USER 65534:65534
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-m", "racelens.recorder.health"]

ENTRYPOINT ["/usr/local/bin/race-lens-recorder"]
