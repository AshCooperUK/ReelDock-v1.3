FROM node:22-bookworm-slim AS frontend
WORKDIR /frontend
RUN npm install --global pnpm@11.19.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build
RUN mkdir -p /usr/local/share/licenses/node && node -e "fetch('https://raw.githubusercontent.com/nodejs/node/'+process.version+'/LICENSE').then(async r=>{if(!r.ok)throw new Error('Node licence unavailable');require('fs').writeFileSync('/usr/local/share/licenses/node/LICENSE',await r.text())}).catch(e=>{console.error(e);process.exit(1)})"

FROM python:3.13-slim AS vpn-native
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY runtime/vpn/socket_mark.c /tmp/socket_mark.c
RUN gcc -shared -fPIC -Wall -Werror -O2 -o /tmp/reeldock_socket_mark.so /tmp/socket_mark.c -ldl

FROM python:3.13-slim
# GHCR links the package to the repository named here; the release workflow
# overrides it with the repository that actually builds the image.
LABEL org.opencontainers.image.source="https://github.com/AshCooperUK/ReelDock-v1.3" \
      org.opencontainers.image.title="ReelDock" \
      org.opencontainers.image.version="1.3.8"
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend /usr/local/share/licenses/node /usr/local/share/licenses/node
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data MEDIA_DIR=/media M3U_DIR=/m3u TZ=Europe/London
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata ffmpeg mesa-va-drivers wireguard-tools iproute2 iptables \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 reeldock && useradd --uid 10001 --gid reeldock --no-create-home reeldock \
    && mkdir -p /data /media /m3u && chown reeldock:reeldock /data /media /m3u && chmod 700 /data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=vpn-native /tmp/reeldock_socket_mark.so /usr/local/lib/reeldock_socket_mark.so
COPY backend ./backend
COPY runtime/vpn/gateway.py ./runtime/vpn/gateway.py
COPY --from=frontend /frontend/dist ./frontend/dist
USER reeldock
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=3)"
CMD ["gunicorn", "--bind", "0.0.0.0:8790", "--workers", "1", "--threads", "4", "--timeout", "60", "backend.app:create_app()"]
