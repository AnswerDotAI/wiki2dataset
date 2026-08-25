FROM rust:1.91-slim-bookworm AS rust

FROM python:3.13-slim-bookworm AS build

COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV PATH=/usr/local/cargo/bin:$PATH RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
RUN apt-get update && apt-get install -y --no-install-recommends gcc libsqlite3-dev patchelf && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir maturin
WORKDIR /src
COPY Cargo.toml pyproject.toml README.md ./
COPY src src
COPY python python
RUN maturin build --release --out /wheels

FROM python:3.13-slim-bookworm

ARG VCS_REF=unknown
ENV WIKI2DATASET_ROOT=/app WIKI2DATASET_COMMIT=$VCS_REF PYTHONUNBUFFERED=1 HF_HUB_DISABLE_PROGRESS_BARS=1
LABEL org.opencontainers.image.source="https://github.com/AnswerDotAI/wiki2dataset"
WORKDIR /app
COPY --from=build /wheels /wheels
COPY Cargo.toml README.md ./
COPY scripts scripts
RUN apt-get update && apt-get install -y --no-install-recommends aria2 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir /wheels/*.whl pyarrow "huggingface_hub>=1.28.0" && rm -rf /wheels

CMD ["python", "-m", "scripts.monthly", "--path", "/data", "--workers", "12"]
