#!/bin/sh
# Image B entrypoint — make sure the ten registered data names exist before node
# starts, without ever touching a file that is already there.
#
# Why this exists
# ---------------
# nitro freezes its public-asset map at BUILD time (see web.Dockerfile), so the
# ten data names are baked into the image as ZERO-BYTE placeholders. A registered
# name whose file is *absent from the mounted volume* is not a 404: the static
# handler finds the key, tries to read the file, and the read throws — the client
# gets HTTP 500 {"error": true, "status": 500, "unhandled": true}. The empty
# volume is exactly the state a FIRST deploy starts in (spec: "a fresh install
# starts with an empty /data/out — the first night run creates the 9 CSVs +
# manifest"), so between the Coolify deploy and the first successful publish every
# data path 500s — including manifest.json, which the site polls. A 500 from a
# static data path reads as a broken deploy and pollutes the logs/health story.
#
# Re-creating a missing name as an empty file restores the build-time state
# exactly: 200 with zero bytes, until the publisher writes real content over it.
#
# Serving matrix measured against the built image (see the same comment in
# deploy/web.Dockerfile):
#   * registered name, file PRESENT in the mount
#       -> 200, served whole and byte-identical, no baked Content-Length
#   * registered name, file ABSENT from the mount
#       -> 500 unhandled WITHOUT this entrypoint; 200 / 0 bytes with it
#   * UNREGISTERED name (e.g. extra_new.csv)
#       -> 307 to /en/data/<name> (the SSR router takes the path)
#
# Invariants
# ----------
# * NEVER truncates or rewrites: a file that exists is left untouched
#   (`[ -f "$f" ] || : > "$f"` semantics; never a bare `> "$f"`). A mounted,
#   already-published CSV must survive the container start byte-for-byte.
# * NEVER blocks boot: if the data directory is read-only (or otherwise
#   unwritable) this logs a warning naming every path it could not create and
#   STILL execs node. The container must never fail to start because of this.
# * PID 1 stays node: `exec "$@"` replaces this shell with the CMD, so signals
#   and the HEALTHCHECK keep working.
#
# The name list is publish.CSV_NAMES plus manifest.json — keep it in step with
# traffic_service/publish.py and with the placeholder block in deploy/build-web.sh.
set -u

DATA_DIR=/app/.output/public/data

NAMES="areas.csv cities.csv color_composition.csv daily.csv hourly.csv hourly_delta.csv monthly.csv road_classes.csv road_classes_hourly.csv manifest.json"

created=""
unwritable=""

for name in $NAMES; do
  f="$DATA_DIR/$name"

  # Present? Leave it alone, whatever its size — never truncate or rewrite a
  # mounted, already-published file.
  if [ -f "$f" ]; then
    continue
  fi

  # Create the missing name as an empty file. The `( : > "$f" )` subshell is
  # load-bearing, not style: `:` is a POSIX SPECIAL built-in, and dash exits the
  # whole script on a redirection error for a special built-in — so a plain
  # `: > "$f"` against a read-only mount KILLED the entrypoint before node ever
  # started (measured: container exited 2, "cannot create …: Read-only file
  # system"). Confined to a subshell that failure is only a non-zero status, and
  # the outer `2>/dev/null` swallows dash's message.
  if ( : > "$f" ) 2>/dev/null; then
    created="${created}${created:+ }$f"
  else
    unwritable="${unwritable}${unwritable:+ }$f"
  fi
done

if [ -n "$created" ]; then
  echo "web-entrypoint: created missing registered data files as empty: $created"
fi

if [ -n "$unwritable" ]; then
  echo "web-entrypoint: WARNING: $DATA_DIR is not writable; could not create: $unwritable" >&2
  echo "web-entrypoint: WARNING: those registered data paths will answer HTTP 500 until the" >&2
  echo "web-entrypoint: WARNING: publisher writes them. Mount a writable volume at $DATA_DIR." >&2
  echo "web-entrypoint: WARNING: starting the server anyway." >&2
fi

exec "$@"
