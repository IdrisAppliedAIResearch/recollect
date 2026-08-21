FROM python:3.13-slim-bookworm

ARG OPENCODE_VERSION=1.18.18

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates nodejs npm \
    && npm install --global "opencode-ai@${OPENCODE_VERSION}" \
    && opencode --version | grep -Fx "${OPENCODE_VERSION}" \
    && python -m pip install --no-cache-dir \
        "httpx==0.28.1" \
        "mcp==1.29.0" \
        "trafilatura==2.2.0" \
    && rm -rf /var/lib/apt/lists/* /root/.npm

RUN groupadd --gid 65532 sandbox \
    && useradd --uid 65532 --gid 65532 --no-create-home sandbox \
    && mkdir -p /opt/recollect/recollect/engine /workspace \
    && touch /opt/recollect/recollect/__init__.py \
    && touch /opt/recollect/recollect/engine/__init__.py \
    && chown 65532:65532 /workspace

COPY src/recollect/engine/mcp_research.py /opt/recollect/recollect/engine/
COPY src/recollect/engine/webtools.py /opt/recollect/recollect/engine/

ENV PYTHONPATH=/opt/recollect \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 65532:65532
WORKDIR /workspace

