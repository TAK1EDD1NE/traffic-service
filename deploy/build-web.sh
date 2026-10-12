#!/usr/bin/env bash
# Build image B — the public site — from a STAGED context.
#
# R30 — the context is a staged copy, never the app directory itself.
# `webapp/lovable-app` holds a Windows-built `node_modules` (plus `.output`,
# `.wrangler`, `.lovable`, `.tanstack`); the Dockerfile runs
# `bun install --frozen-lockfile` and then `COPY . .`, so a raw
# `docker build <app dir>` would copy that host tree over the container's Linux
# node_modules and break the build — besides shipping hundreds of MB of context.
# `stage()` below assembles a clean copy. The Dockerfile travels with the service
# repo (this directory) and is passed with -f.
#
# Why the image is built here and pushed to GHCR instead of built on the school
# VPS: image B carries the app's whole dependency tree, and the VPS should not
# re-download it. The app directory is Lovable-managed, so every deploy artefact
# (Dockerfile, build script, compose, runbook) lives in `traffic-service/deploy/`.
#
# Usage: deploy/build-web.sh [tag] [--push]         (default tag: latest)
#        deploy/build-web.sh --stage-only <dir>
#
#   --stage-only <dir>  Assemble the docker build context into <dir> (the app copy
#                       with its exclusions, the nine zero-byte CSV placeholders,
#                       the runtime entrypoint, the dotenv/CSV/node_modules
#                       assertions), print the staged file count, and exit WITHOUT
#                       running `docker build`. This makes the staging — the part
#                       that decides what a pushed image can contain — provable in
#                       a second instead of a multi-minute image build.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
app=$(cd "$repo/../webapp/lovable-app" && pwd)

stage_only=""
tag=latest
push=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stage-only)
      [ $# -ge 2 ] || { echo "--stage-only needs a directory" >&2; exit 2; }
      stage_only=$2
      shift 2
      ;;
    --push)
      push=--push
      shift
      ;;
    --*)
      echo "unknown option: $1" >&2
      exit 2
      ;;
    *)
      tag=$1
      shift
      ;;
  esac
done
image="ghcr.io/tak1edd1ne/traffic-web:${tag}"

# stage <dest> — assemble the docker build context under <dest>.
stage() {
  local dest=$1
  mkdir -p "$dest"

  # Exclude the host's Windows-built dependency/build trees (`node_modules`,
  # `.output`, `.wrangler`, `.lovable`, `.tanstack`) and the app's own git dir.
  # The patterns carry NO leading `./` ON PURPOSE — see the same note in
  # build-worker.sh: unanchored, `node_modules` and any dotenv pattern match at
  # ANY depth, so a nested `node_modules` or a dotenv file down a subtree is
  # dropped too, not just a top-level one. The app is a Lovable/Supabase project,
  # so a `.env*` would hold service keys and must never reach an image pushed to
  # GHCR. No such file exists in the app today; the exclusion is unconditional.
  #
  # `public/data` is excluded on top of that list. The app commits its own
  # nine-CSV bundle there and `bun run build` copies `public/` into
  # `.output/public/`, so `COPY . .` would otherwise bake a day-flavoured snapshot
  # of the exact files the shared volume is supposed to supply. The mounted volume
  # would shadow it — until the volume went missing, and then the container would
  # silently serve stale data instead of failing. The CSVs are never baked in
  # (spec:76-79); the placeholder block below is what the image carries instead.
  tar -C "$app" \
      --exclude=node_modules --exclude=.output --exclude=.wrangler \
      --exclude=.lovable --exclude=.tanstack --exclude=.git \
      --exclude=public/data --exclude=./public/data \
      --exclude=.env --exclude='.env.*' \
      -cf - . | tar -C "$dest" -xf -

  # The nine published CSVs get ZERO-BYTE placeholders, never the app's committed
  # bundle. Two reasons, the second fatal:
  #   1. the app commits its own nine-CSV snapshot in `public/data`; `bun run build`
  #      copies `public/` into `.output/public/`, so `COPY . .` would bake a
  #      day-flavoured copy of the exact data the shared volume exists to own — and
  #      shadowing hides it only until the volume is absent, when stale data serves.
  #   2. (fatal) nitro freezes `public_assets_data_default` at BUILD time: a map of
  #      pathname -> {type, etag, mtime, size, path}. Its static handler serves a path
  #      only if the pathname is a KEY in that map, and it sets `Content-Length` from
  #      the recorded `size` while reading the bytes from disk at request time. A path
  #      absent from the map falls through to the SSR router (which 307-redirects
  #      `/data/cities.csv` to `/en/data/cities.csv`), and a mounted file whose size
  #      differs from the recorded one is mis-served (truncated or empty). Both were
  #      measured against the built image, not theorised.
  #   `size: 0` is therefore load-bearing: with the recorded size zero, the handler
  #   sets no Content-Length and node streams the mounted file whole, whatever its
  #   size. The nine names must exist as empty files at build time; their CONTENT
  #   never does — the mount is the only source.
  mkdir -p "$dest/public/data"
  for name in areas cities color_composition daily hourly hourly_delta monthly \
              road_classes road_classes_hourly; do
    : > "$dest/public/data/$name.csv"
  done
  : > "$dest/public/data/manifest.json"

  # The runtime entrypoint (see web.Dockerfile) travels with the service repo in
  # this directory. docker's COPY can only reach files inside the build context,
  # and the context is this staged copy of the app — so the entrypoint is staged
  # at the context root beside it. It is the one staged file that is not the app.
  cp "$repo/deploy/web-entrypoint.sh" "$dest/web-entrypoint.sh"

  # The tar --exclude above is the PATTERN; this is the GUARANTEE. A pattern can
  # be narrowed by an anchor, a tar whose glob semantics differ, or a future edit —
  # and a single missed depth would bake a real credential into a pushed image.
  local leaked
  leaked=$(find "$dest" -name '.env*' -print)
  if [ -n "$leaked" ]; then
    echo "staged context still contains dotenv files (must be excluded at any depth):" >&2
    printf '%s\n' "$leaked" >&2
    exit 1
  fi

  # Same discipline for the CSVs: the exclude above is the pattern, this is the
  # guarantee. Only the nine ZERO-BYTE placeholders may exist — any CSV with bytes in
  # it is the published dataset leaking into a pushed image, which is the one thing
  # the mount exists to make impossible.
  local baked
  baked=$(find "$dest" -name '*.csv' -size +0c -print)
  if [ -n "$baked" ]; then
    echo "staged context carries CSV DATA (only zero-byte placeholders may be baked):" >&2
    printf '%s\n' "$baked" >&2
    exit 1
  fi

  # A staged `node_modules` would defeat the whole point of the stage: the
  # Dockerfile's `COPY . .` would drop the host's Windows binaries into the Linux
  # image. Whatever the tar matched, refuse it.
  local nm
  nm=$(find "$dest" -name node_modules -print -prune)
  if [ -n "$nm" ]; then
    echo "staged context contains node_modules (host tree must not enter the image):" >&2
    printf '%s\n' "$nm" >&2
    exit 1
  fi

  # The entrypoint is the one file the Dockerfile COPYs from the context root. A
  # missing or empty one would fail the image build late (or, worse, ship a
  # no-op); refuse it here, in the second the stage takes.
  if [ ! -s "$dest/web-entrypoint.sh" ]; then
    echo "staged context is missing deploy/web-entrypoint.sh (the runtime entrypoint):" >&2
    echo "  $dest/web-entrypoint.sh" >&2
    exit 1
  fi
}

if [ -n "$stage_only" ]; then
  mkdir -p "$stage_only"
  stage "$stage_only"
  count=$(find "$stage_only" -type f | wc -l)
  echo "staged $count files in $stage_only"
  exit 0
fi

stage_dir=$(mktemp -d)
trap 'rm -rf "$stage_dir"' EXIT
stage "$stage_dir"

# MSYS path conversion is off on this box: `docker` is a native binary and needs
# `C:/…` paths, so both the -f path and the context go through cygpath.
native() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
docker build -f "$(native "$repo/deploy/web.Dockerfile")" \
             -t "$image" "$(native "$stage_dir")"

if [ "$push" = "--push" ]; then docker push "$image"; fi
echo "built $image"
