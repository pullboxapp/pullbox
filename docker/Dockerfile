FROM dhi.io/python:3.14-debian13-dev AS builder

ARG UNRAR_SOURCE_URL="https://www.rarlab.com/rar/unrarsrc-7.2.6.tar.gz"
ARG UNRAR_SOURCE_SHA256="d1afa67ef4121ebc5986815699e05db0ce8648499e5dca854f282a4c3f72c003"

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:/opt/python/bin:${PATH}" \
    PIP_NO_COMPILE=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        gzip \
        poppler-utils \
        p7zip-full \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSL "${UNRAR_SOURCE_URL}" -o /tmp/unrarsrc.tar.gz; \
    echo "${UNRAR_SOURCE_SHA256}  /tmp/unrarsrc.tar.gz" | sha256sum -c -; \
    mkdir -p /tmp/unrar-src; \
    tar -xzf /tmp/unrarsrc.tar.gz -C /tmp/unrar-src --strip-components=1; \
    make -C /tmp/unrar-src -j"$(nproc)" unrar; \
    install -m 0755 /tmp/unrar-src/unrar /usr/bin/unrar; \
    install -D -m 0644 /tmp/unrar-src/license.txt /usr/share/licenses/unrar/license.txt; \
    rm -rf /tmp/unrarsrc.tar.gz /tmp/unrar-src

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN python -m venv "${VIRTUAL_ENV}" \
    && "${VIRTUAL_ENV}/bin/python" -m pip install --upgrade pip wheel \
    && "${VIRTUAL_ENV}/bin/python" -m pip install --no-cache-dir --no-compile ".[prod]" \
    && find "${VIRTUAL_ENV}" -type d -name __pycache__ -prune -exec rm -rf {} +

RUN set -eu; \
    mkdir -p /runtime-root/usr/bin /runtime-root/usr/lib /runtime-root/usr/share \
        /runtime-root/etc/ssl /runtime-root/etc/ca-certificates \
        /runtime-dirs/data/logs /runtime-dirs/data/backups /runtime-dirs/data/tmp \
        /runtime-dirs/comics/.covers /runtime-dirs/downloads /runtime-dirs/imports; \
    copy_path() { \
        src="$1"; \
        case "${src}" in \
            /lib64/*) return 0 ;; \
            /lib/*) dest="/runtime-root/usr/lib/${src#/lib/}" ;; \
            *) dest="/runtime-root${src}" ;; \
        esac; \
        mkdir -p "$(dirname "${dest}")"; \
        cp -aL "${src}" "${dest}"; \
    }; \
    copy_binary() { \
        binary="$1"; \
        copy_path "${binary}"; \
        ldd "${binary}" \
            | while read -r left arrow path _rest; do \
                if [ "${arrow}" = "=>" ] && [ -f "${path}" ]; then \
                    copy_path "${path}"; \
                elif [ -f "${left}" ]; then \
                    copy_path "${left}"; \
                fi; \
            done; \
    }; \
    copy_binary /usr/bin/unrar; \
    copy_binary /usr/bin/pdfinfo; \
    copy_binary /usr/bin/pdftoppm; \
    copy_binary /usr/lib/7zip/7z; \
    cp -aL /usr/lib/7zip /runtime-root/usr/lib/; \
    install -D -m 0644 /usr/share/licenses/unrar/license.txt /runtime-root/usr/share/licenses/unrar/license.txt; \
    ln -s /usr/lib/7zip/7z /runtime-root/usr/bin/7z; \
    cp -a /etc/ssl/certs /runtime-root/etc/ssl/; \
    cp -a /etc/ca-certificates /runtime-root/etc/; \
    cp -a /usr/share/ca-certificates /runtime-root/usr/share/; \
    cp -a /usr/share/zoneinfo /runtime-root/usr/share/; \
    if [ -d /usr/share/poppler ]; then cp -a /usr/share/poppler /runtime-root/usr/share/; fi

FROM dhi.io/python:3.14-debian13 AS runtime

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/python/bin:/usr/bin:/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/bin" \
    PYTHONPATH="/opt/venv/lib/python3.14/site-packages" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PULLBOX_DB_URL="sqlite+aiosqlite:////data/pullbox.db"

WORKDIR /app

COPY --from=builder /runtime-root/ /
COPY --from=builder --chown=65532:65532 /runtime-dirs/data /data
COPY --from=builder --chown=65532:65532 /runtime-dirs/comics /comics
COPY --from=builder --chown=65532:65532 /runtime-dirs/downloads /downloads
COPY --from=builder --chown=65532:65532 /runtime-dirs/imports /imports
COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv
COPY --chown=65532:65532 alembic/ alembic/

VOLUME /data

ARG BUILD_DATE
ARG GIT_SHA
ARG GIT_BRANCH
ARG VERSION
ENV PULLBOX_BUILD_DATE="${BUILD_DATE}" \
    PULLBOX_GIT_SHA="${GIT_SHA}" \
    PULLBOX_GIT_BRANCH="${GIT_BRANCH}" \
    PULLBOX_BUILD_VERSION="${VERSION}"
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="Pullbox" \
      org.opencontainers.image.description="Modern comic book management and acquisition platform"

USER 65532:65532

EXPOSE 8585

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD ["python", "-m", "pullbox.docker_healthcheck"]

ENTRYPOINT ["python", "-m", "pullbox.docker_entrypoint"]
CMD ["python", "-m", "pullbox"]
