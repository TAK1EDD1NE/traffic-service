#!/usr/bin/env bash
# Build image A from a staged context.
#
# `data engineering/` is tracked by no repository and contains a 19.5 GB corpus,
# so the context is assembled here: this repo + exactly the five files the
# service imports or reads at runtime. The webapp is not involved.
#
# Usage: deploy/build-worker.sh [tag] [--push]      (default tag: latest)
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
root=$(cd "$repo/.." && pwd)
tag=${1:-latest}
push=${2:-}
image="ghcr.io/tak1edd1ne/traffic-worker:${tag}"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/traffic-service" "$stage/data-engineering"

# Stage the repo. `tar` ignores `.gitignore`, so `.env` and every `.env.*` variant are excluded
# here explicitly: developers keep a live (gitignored) `traffic-service/.env` holding real DB
# credentials, and this context is COPYed into an image that gets pushed to GHCR. Nothing in the
# image reads a dotenv file — configuration arrives as Coolify env vars via `os.environ` — so
# dropping them costs nothing and keeps the plan's binding rule: secrets live in env vars, never
# in the repo or the image.
tar -C "$repo" \
    --exclude=./.git --exclude=./.venv --exclude=./.pytest_cache \
    --exclude=./.superpowers --exclude='*/__pycache__' \
    --exclude=./.env --exclude='./.env.*' \
    -cf - . | tar -C "$stage/traffic-service" -xf -

for f in google_maps_scraper.py run_traffic_local.py regenerate_lovable_csv.py \
         locations.json traffic_areas.csv; do
  cp "$root/data engineering/$f" "$stage/data-engineering/$f"
done

# The regenerator must be the parameterised copy (plan 3, Task 5) or publish
# aborts at runtime — fail the build instead, while a human is watching. Check
# ALL THREE names the runtime guard (`publish._assert_regenerator_supports_env`)
# asserts: a copy that honours only some of them would pass a single-name check
# here and still abort at runtime, the exact outcome this guard exists to prevent.
guard_missing=""
for name in TRAFFIC_CSV_BASE TRAFFIC_CSV_OUT TRAFFIC_POPULATIONS_CSV; do
  grep -q "$name" "$stage/data-engineering/regenerate_lovable_csv.py" \
    || guard_missing="$guard_missing $name"
done
if [ -n "$guard_missing" ]; then
  echo "regenerate_lovable_csv.py does not honour:$guard_missing — ship the parameterised copy (plan 3, Task 5)" >&2
  exit 1
fi

# MSYS path conversion is off on this box: `docker` is a native binary and needs
# `C:/…` paths, so both the -f path and the context go through cygpath.
native() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
docker build -f "$(native "$stage/traffic-service/deploy/worker.Dockerfile")" \
             -t "$image" "$(native "$stage")"

if [ "$push" = "--push" ]; then docker push "$image"; fi
echo "built $image"
