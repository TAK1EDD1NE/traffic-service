#!/usr/bin/env bash
# Build image A from a staged context.
#
# `data engineering/` is tracked by no repository and contains a 19.5 GB corpus,
# so the context is assembled here: this repo + exactly the five files the
# service imports or reads at runtime. The webapp is not involved.
#
# Usage: deploy/build-worker.sh [tag] [--push]      (default tag: latest)
#        deploy/build-worker.sh --stage-only <dir>
#
#   --stage-only <dir>  Assemble the docker build context into <dir> (the repo
#                       tar with its exclusions, the five `data engineering/`
#                       files, the regenerator guard, the dotenv assertion),
#                       print the staged file count, and exit WITHOUT running
#                       `docker build`. This makes the staging — the part that
#                       decides what a pushed image can contain — provable in a
#                       second instead of a multi-GB image build.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
root=$(cd "$repo/.." && pwd)

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
image="ghcr.io/tak1edd1ne/traffic-worker:${tag}"

# stage <dest> — assemble the docker build context under <dest>.
stage() {
  local dest=$1
  mkdir -p "$dest/traffic-service" "$dest/data-engineering"

  # Stage the repo. `tar` ignores `.gitignore`, so dotenv files are excluded here
  # explicitly: developers keep a live (gitignored) `.env` holding real DB
  # credentials, and this context is COPYed into an image that gets pushed to GHCR.
  # The patterns carry NO leading `./` ON PURPOSE — a `--exclude=./.env` is
  # anchored to the context root and drops only a TOP-LEVEL `.env`, leaving
  # `deploy/.env`, `sub/.env.prod`, `deep/deeper/.env` and `.env.bak` staged (plan 4
  # Task 4 writes a real `deploy/.env`, Task 5 puts ALERT_WEBHOOK_URL there, and
  # `deploy/` is inside this context; `deploy/worker.Dockerfile` COPYs the whole
  # tree into a pushed image). Unanchored, `.env` and `.env.*` match at ANY depth.
  # Nothing in the image reads a dotenv file — configuration arrives as Coolify env
  # vars via `os.environ` — so dropping them all costs nothing and keeps the plan's
  # binding rule: secrets live in env vars, never in the repo or the image.
  tar -C "$repo" \
      --exclude=./.git --exclude=./.venv --exclude=./.pytest_cache \
      --exclude=./.superpowers --exclude='*/__pycache__' \
      --exclude=.env --exclude='.env.*' \
      -cf - . | tar -C "$dest/traffic-service" -xf -

  for f in google_maps_scraper.py run_traffic_local.py regenerate_lovable_csv.py \
           locations.json traffic_areas.csv; do
    cp "$root/data engineering/$f" "$dest/data-engineering/$f"
  done

  # The regenerator must be the parameterised copy (plan 3, Task 5) or publish
  # aborts at runtime — fail the build instead, while a human is watching. Check
  # ALL THREE names the runtime guard (`publish._assert_regenerator_supports_env`)
  # asserts: a copy that honours only some of them would pass a single-name check
  # here and still abort at runtime, the exact outcome this guard exists to prevent.
  local guard_missing=""
  local name
  for name in TRAFFIC_CSV_BASE TRAFFIC_CSV_OUT TRAFFIC_POPULATIONS_CSV; do
    grep -q "$name" "$dest/data-engineering/regenerate_lovable_csv.py" \
      || guard_missing="$guard_missing $name"
  done
  if [ -n "$guard_missing" ]; then
    echo "regenerate_lovable_csv.py does not honour:$guard_missing — ship the parameterised copy (plan 3, Task 5)" >&2
    exit 1
  fi

  # The tar --exclude above is the PATTERN; this is the GUARANTEE. A pattern can be
  # narrowed by an anchor, a tar whose glob semantics differ, or a future edit — and
  # a single missed depth would bake a real credential into an image pushed to GHCR.
  # Whatever the tar matched, refuse to build a context that still carries a dotenv
  # file: any `.env*` at any depth is a defect, never tolerated silently.
  local leaked
  leaked=$(find "$dest" -name '.env*' -print)
  if [ -n "$leaked" ]; then
    echo "staged context still contains dotenv files (must be excluded at any depth):" >&2
    printf '%s\n' "$leaked" >&2
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
docker build -f "$(native "$stage_dir/traffic-service/deploy/worker.Dockerfile")" \
             -t "$image" "$(native "$stage_dir")"

if [ "$push" = "--push" ]; then docker push "$image"; fi
echo "built $image"
