# syntax=docker/dockerfile:1.7
# Image B — the public site, built for node.
#
# Build context: a STAGED copy of `webapp/lovable-app`, assembled by
# `deploy/build-web.sh` (this file travels with the service repo and is passed
# with -f). A raw `docker build <app dir>` is broken: that directory carries a
# Windows-built `node_modules` (and `.output`/`.wrangler`/`.lovable`/`.tanstack`),
# and the `COPY . .` below would drop the host tree over the container's Linux
# one — so the script stages a clean context first (R30).
#
# Why this image is built on the dev box and pushed to GHCR rather than built on
# the school VPS: image B carries the app's whole dependency tree (bun install +
# base-image layers), and the VPS should not re-download it. The app directory is
# Lovable-managed, so every deploy artefact — this Dockerfile, `build-web.sh`, the
# compose file, the runbook — lives in `traffic-service/deploy/` instead.
#
# NITRO_PRESET=node-server beats the Lovable config's `cloudflare-module`
# defaultPreset, and leaves vite.config.ts untouched so Lovable's own builds keep
# their Cloudflare target (docs in @lovable.dev/vite-tanstack-config index.d.ts).
#
# No CSV content is baked in. The app ships its own committed `public/data/*.csv`
# bundle, but `deploy/build-web.sh` excludes it and stages the nine published names
# as ZERO-BYTE placeholders instead: nitro freezes its public-asset map at build
# time, so the names must be present for `/data/*` to be served at all, while a
# recorded `size: 0` keeps nitro from setting a Content-Length that a differently
# sized mounted file would be truncated to. Content is read from the mount at request
# time, so the shared volume at /app/.output/public/data is the ONLY source of data:
# a new day is a file write, never a rebuild (spec:76-79), and with no volume mounted
# the CSVs come back empty rather than stale.
FROM oven/bun:1 AS build
WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
RUN bun install --frozen-lockfile
COPY . .
RUN NITRO_PRESET=node-server bun run build \
 && test -f .output/server/index.mjs \
 && mkdir -p .output/public/data

FROM node:22-slim AS runtime
ENV NODE_ENV=production \
    PORT=3000 \
    TZ=Africa/Algiers
WORKDIR /app
COPY --from=build /app/.output ./.output
RUN mkdir -p /app/.output/public/data
VOLUME ["/app/.output/public/data"]
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD node -e "fetch('http://127.0.0.1:3000/en').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"
CMD ["node", ".output/server/index.mjs"]
