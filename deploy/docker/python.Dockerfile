FROM ghcr.io/astral-sh/uv:0.9.11-python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY . .
RUN uv sync --all-packages --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# The commit the image is built from: platform_version is `<project version>+<EAP_BUILD_SHA>` (`+dev` without it).
ARG EAP_BUILD_SHA=dev
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EAP_BUILD_SHA=$EAP_BUILD_SHA \
    EAP_SYSTEM_MANIFEST_PATH=/opt/eap/system-manifest.yaml
RUN apt-get update \
    && apt-get install --no-install-recommends --yes postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 eap \
    && useradd --system --uid 10001 --gid eap --home-dir /app --shell /usr/sbin/nologin eap
WORKDIR /app
COPY --from=builder --chown=eap:eap /app /app
# The packaged system manifest: the worker reads it from here (WorkerSettings.system_manifest_path), no network.
COPY --chown=eap:eap docs/platform/system-manifest.yaml /opt/eap/system-manifest.yaml
COPY --chown=eap:eap deploy/docker/python-entrypoint.sh /usr/local/bin/eap-entrypoint
RUN chmod 0555 /usr/local/bin/eap-entrypoint
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["eap-entrypoint"]
CMD ["api"]
