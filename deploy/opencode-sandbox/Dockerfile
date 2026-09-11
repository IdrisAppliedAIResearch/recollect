FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS opencode-build

ARG OPENCODE_VERSION=1.18.18
ARG OPENCODE_INTEGRITY=sha512-J+5HFq8tf+wPBBpBpMPSNjSytF2/EkNWYfFZh4si1d9auFbQriqDyqZv+vFUsLWERfdMU32Eajwuiq3rKBvZLQ==

RUN test "$(npm view "opencode-ai@${OPENCODE_VERSION}" dist.integrity)" \
        = "${OPENCODE_INTEGRITY}" \
    && npm install --global "opencode-ai@${OPENCODE_VERSION}" \
    && opencode --version | grep -Fx "${OPENCODE_VERSION}" \
    && cp --dereference /usr/local/bin/opencode /opt/opencode \
    && chmod 0755 /opt/opencode \
    && rm -rf /root/.npm

FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1 AS python-build

COPY deploy/opencode-sandbox/requirements.lock /tmp/requirements.lock
COPY deploy/opencode-sandbox/fetch-ripgrep.py /tmp/fetch-ripgrep.py

# Native skill loading enumerates supporting files with rg. Runtime downloads
# land on noexec scratch, so keep the verified binary in the read-only image.
RUN python /tmp/fetch-ripgrep.py && /opt/rg --version

RUN python -m pip install --no-cache-dir --require-hashes \
        --target /opt/python -r /tmp/requirements.lock \
    && find /opt/python -type d \
        \( -name __pycache__ -o -name tests -o -name test \) \
        -prune -exec rm -rf '{}' +

FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

COPY --from=opencode-build /opt/opencode /usr/local/bin/opencode
COPY --from=python-build /opt/python /opt/python
COPY --from=python-build /opt/rg /usr/local/bin/rg
COPY --from=python-build /opt/ripgrep-notices /usr/share/doc/ripgrep

RUN groupadd --gid 65532 sandbox \
    && useradd --uid 65532 --gid 65532 --no-create-home sandbox \
    && mkdir -p /opt/recollect/recollect/engine /workspace \
    && touch /opt/recollect/recollect/__init__.py \
    && touch /opt/recollect/recollect/engine/__init__.py \
    && chown 65532:65532 /workspace

COPY src/recollect/engine/mcp_research.py /opt/recollect/recollect/engine/
COPY src/recollect/engine/webtools.py /opt/recollect/recollect/engine/

ENV PYTHONPATH=/opt/python:/opt/recollect \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 65532:65532
WORKDIR /workspace
