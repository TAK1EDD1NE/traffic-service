# traffic-index deploy runbook

Operator procedure for the live traffic index: from an empty VPS to a serving site, and
from a running one to the next release. Every command block is runnable **as written from
the repo root** (`traffic-service/`, the directory this file's `deploy/` sits under) unless
a block says otherwise; every verification step states the output it expects.

The deploy is one Coolify **Docker Compose resource** named `traffic-index`
(`deploy/compose.prod.yml`) with **three** containers — `db`, `worker`, `web` — one shared
data volume, and no published ports. The images are **built and pushed from the dev box**,
never built on the VPS: image A carries a baked Chromium, and the frontend app directory is a
repository of its own — Lovable-synced, not a clone of this one (§3) — which is why a raw
`docker build` of it is never run. Coolify only pulls and runs.

---

## 1. What runs where

| Container | Image | Role | Restart | Ports |
|---|---|---|---|---|
| `db` | `postgres:16-alpine` | the database (`measurements`, the rollups, `slots`, `captures`, `job_runs`, `zones`) | `always` | none — `pg_isready` healthcheck only |
| `worker` | `TRAFFIC_WORKER_IMAGE` (image A) | capture + `night` + `maintenance`; runs the scheduler | `always` | none; **no `HEALTHCHECK`** — `ps` shows `running`, never `healthy` |
| `web` | `TRAFFIC_WEB_IMAGE` (image B) | the public site (Nitro node-server on `3000`) | `always` | none — Coolify/Traefik terminates TLS and routes the domain here; the image also carries its own in-container `HEALTHCHECK` (`GET /en`) |

There are **no published ports on any service**: the public site reaches `web` through
Traefik on the internal `3000`, Postgres is never exposed, and nothing else is reachable
from the public IP.

**The shared volume.** `traffic-data` is mounted into both `worker` and `web`:

    worker: /data                    (the volume ROOT: /data/raw, /data/out, /data/dump)
    web:    /app/.output/public/data (the volume SUBPATH `out` only)

The subpath is deliberate and load-bearing. The site's data root must hold exactly the nine
published CSVs and `manifest.json`; mounting the volume root there would put `raw/`, `out/`
and `dump/` under the data root and serve nothing at `/data/*.csv` — and publishing into the
volume root instead would expose `/data/raw/<slot>/<png>` and `/data/dump/<day>.sql.gz` (a
downloadable database dump) on the public site. The volume's `out/` subpath is the **only**
channel between the publisher and the site.

    /data/raw/<YYYY-MM-DD>T<HH>-00/<zone_id>_<local slot>.png   capture writes here; pruned after 48 h
    /data/out/{9 CSVs, manifest.json}                           the publish writes here; the site serves it
    /data/dump/<YYYY-MM-DD>.sql.gz                              the nightly pg_dump; 14 kept

`traffic-pgdata` is a second volume — the Postgres data directory, entirely separate from
`traffic-data`.

**The job schedule** (one long-lived `scheduler` process in `worker`, TZ `Africa/Algiers`,
jobs run sequentially on a single worker):

| When (local) | Job |
|---|---|
| every even hour `00:00 … 22:00` | `capture` — one slot: the **89** `publish = true` zones (`capture.load_zones` filters; the registry's other 6 are the quiet areas) |
| `00:30` | `night` — close **yesterday**: measure → aggregate → validate → publish → prune |
| `03:30` | `maintenance` — `pg_dump`, prune raw PNGs, `VACUUM`, health + disk report |

14 jobs in total (12 capture slots + night + maintenance).

**Disk budget.** The two image layers on disk total about **5 GB** — image A is **~4.64 GB**
(4,644,940,379 bytes; the Playwright base plus a baked Chromium, by design) and image B is
**~331 MB** (331,049,864 bytes). The spec's data budget sits **on top** of that: the 48 h
window of raw PNGs peaks around **2.4 GB** (≈1.19 GB/day × 2 days; 12 slots × the 89 captured
zones = **1,068 images a day**), plus
Postgres (≈0.3 MB/day of `measurements`), the 14 nightly dumps, and container logs — roughly
**7 GB steady and ~10 GB peak**. So the full footprint is about **12 GB steady / ~15 GB peak**.
A **40 GB SSD** is comfortable, but watch it at the peak: with ~5 GB of images that never
move, the raw window is the variable, and the disk alert fires at **80 %** (§8).

---

## 2. Prerequisites

- **VPS**: 2 vCPU / 4 GB RAM floor, with **Docker** and **Coolify** installed. This is a
  shared school machine — the deploy owns only its own Compose project and volumes; nothing
  system-wide is reconfigured beyond Docker + Coolify.
- **A GHCR account** for the images. They are **private by default**, so the VPS must be able
  to authenticate to pull them. Have a **PAT with `read:packages`** for the pulling side (and
  `write:packages` on the dev box that pushes).
- **A DNS name** for the site, pointing at the VPS, with a Coolify-managed TLS certificate
  (Let's Encrypt needs a name). The hostname is the user's open provisioning item — the
  school domain, or an `sslip.io` / cheap domain — but the resource needs one before deploy.
- **An alert endpoint** (an ntfy topic or any URL that accepts a plain-text POST) for
  `ALERT_WEBHOOK_URL` (§8). Empty is allowed: alerts are then logged and dropped, not sent.

---

## 3. Build and push on the dev box

Both images are **built and pushed from the dev box**. Tags are the convention
**`<YYYYMMDD>-<short commit>`** (e.g. `20261012-663524d`); `latest` is the fallback the
Compose defaults name, but pin an explicit tag in the Coolify env so a redeploy is
reproducible.

    # from the repo root (traffic-service/)
    git rev-parse --short HEAD                 # the short commit for the tag
    tag="$(date +%Y%m%d)-$(git rev-parse --short HEAD)"

    # authenticate once, then build and push both images
    docker login ghcr.io -u TAK1EDD1NE         # paste the PAT (write:packages) at the prompt
    bash deploy/build-worker.sh "$tag" --push  # image A, ~4.64 GB — several minutes
    bash deploy/build-web.sh    "$tag" --push  # image B, ~331 MB

Expected tail of each: `built ghcr.io/tak1edd1ne/traffic-worker:<tag>` /
`built ghcr.io/tak1edd1ne/traffic-web:<tag>`. Confirm what landed:

    docker image inspect ghcr.io/tak1edd1ne/traffic-worker:"$tag" --format '{{.Id}} {{.Size}}'
    docker image inspect ghcr.io/tak1edd1ne/traffic-web:"$tag"    --format '{{.Id}} {{.Size}}'

Expected: image A ≈ `4644940379` bytes, image B ≈ `331049864` bytes. Both scripts take
`--stage-only <dir>` to assemble and prove the build context in seconds **without** running
the multi-GB `docker build` (it prints `staged N files in <dir>`) — useful when you only
changed the Dockerfile or want to confirm no secrets are staged. The scripts refuse to build
a context that carries any `.env*` at any depth, any non-empty CSV (image B), or a
`node_modules` (image B).

> Image A is **~4.64 GB by design**. The Chromium browser is baked into the image so the
> school VPS never compiles or downloads one; the brief's older "~2.5 GB" figure is stale —
> measure with the `docker image inspect` above. Image B is built with
> `NITRO_PRESET=node-server` and stages the app dir as a clean copy (the app dir is its own
> Lovable-synced repository with a Windows-built `node_modules`; a raw `docker build` of it
> would copy that tree into the image). Nothing is executed on the app repo by this runbook.

---

## 4. GHCR auth on the VPS

Coolify pulls the images, so the **VPS** needs read access to GHCR. Either add the
credentials in **Coolify → Servers → Registry** (or per resource, "Private registry") with:

    registry:  ghcr.io
    username:  TAK1EDD1NE
    token:     <PAT with read:packages>

or log the Docker daemon in once over SSH:

    ssh <vps>
    docker login ghcr.io -u TAK1EDD1NE         # same PAT class: read:packages is enough

Verify the pull works **before** deploying (the same tag you pushed in §3):

    docker pull ghcr.io/tak1edd1ne/traffic-worker:<tag>

Expected: `Status: Downloaded newer image…` (or `Image is up to date`). A `denied` /
`unauthorized` here is a credentials problem — fix it in Coolify's registry settings, not on
the dev box. If the VPS **cannot reach GHCR at all** (network policy), see §11 —
"the VPS cannot reach GHCR".

---

## 5. The Coolify resource

1. **New resource → Docker Compose**. Point it at the repository
   `TAK1EDD1NE/traffic-service` and branch **`feat/night-pipeline`** (or `main` once the
   user merges). Set **Compose location** to `deploy/compose.prod.yml`.
2. **Environment variables** — copy `deploy/.env.example` and set the same keys in Coolify's
   env UI. No secret may live in the repo.

       POSTGRES_USER=traffic
       POSTGRES_PASSWORD=<a strong, URL-SAFE password — see the warning below>
       POSTGRES_DB=traffic
       ALERT_WEBHOOK_URL=<your ntfy topic or POST endpoint>   # may be empty
       TRAFFIC_WORKER_IMAGE=ghcr.io/tak1edd1ne/traffic-worker:<tag>
       TRAFFIC_WEB_IMAGE=ghcr.io/tak1edd1ne/traffic-web:<tag>

   `POSTGRES_USER` / `POSTGRES_DB` / `POSTGRES_PASSWORD` are consumed by **both** `db` and
   the `worker`'s `DATABASE_URL`. The two image keys are the pins — set explicit tags, not
   `latest`.
3. **Domain**: set the domain on the **`web`** service only. Coolify/Traefik terminates TLS
   and routes to `web:3000`; there is nothing to publish.
4. **Deploy.** Watch the build/pull, then follow the logs:

       docker compose -f deploy/compose.prod.yml --env-file <env> logs -f worker
       # on the VPS the Coolify-generated .env is the env file; the same compose file is used
       # either way. Run these over SSH in the resource's directory, or use Coolify's
       # Terminal, which drops you into a shell with the stack's env already exported.

   Expected: `scheduler started; capture at 00:00, 02:00, … 22:00; night at 00:30;
   maintenance at 03:30 Africa/Algiers`, and `db` logging `database system is ready to
   accept connections`.

> `worker` runs the scheduler as its **default command** — do not override the CMD with a
> one-off. One-shot operator commands are run with `docker compose run --rm` (§6, §9).

### Restrict the Coolify dashboard (do this before you expose the VPS)

The Coolify dashboard must **not** sit open on the public IP (spec:227-228). Do one of:

- **basic auth + an IP allowlist** in front of the dashboard; or
- an **SSH port-forward** (`ssh -L 8000:127.0.0.1:8000 <vps>` then browse
  `http://127.0.0.1:8000`); or
- a **Cloudflare Tunnel** in front of it.

Only the site's own domain should be reachable from the public internet.

---

## 6. First bring-up

Everything here runs from the repo root with the same compose file and the same env, so every
command below is written out in full as
`docker compose -f deploy/compose.prod.yml --env-file <env> …`.

Substitute `<env>` with the Coolify env file path (or `deploy/.env` locally). On the VPS,
Coolify's Terminal already has the env exported — `docker compose -f deploy/compose.prod.yml …`
is then enough.

### Step 1 — bootstrap the volume (DO THIS FIRST, fresh volume only)

Bring the database up first (the worker's one-shots depend on it being healthy):

    docker compose -f deploy/compose.prod.yml --env-file <env> up -d --wait db
    # expected: Container traffic-index-db-1 Started / Healthy

On a **fresh `traffic-data` volume**, `web` **cannot start**: it mounts the volume's `out/`
**subpath**, and Docker refuses a subpath mount whose directory does not exist. The engine
error is

    cannot access path /var/lib/docker/volumes/traffic-index_traffic-data/_data/out:
    lstat …: no such file or directory

and because `web` is `restart: always`, that refusal becomes a **crash-loop** until `out/`
exists. Nothing in `web` creates it — only a successful `night` publish does. Create the
tree once, before `web`'s first start, in the same session as the migration steps:

    # one-liner baked into the compose comment (deploy/compose.prod.yml) and .env.example:
    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
      sh -c 'mkdir -p /data/out /data/raw /data/dump'

Expected: the container runs, creates nothing it needs to print, and exits `0`. Verify:

    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
      sh -c 'ls -la /data'

Expected: `out/`, `raw/`, `dump/` each `drwxr-xr-x … root root` under `/data`.

> **How to recognise the failure:** if you deploy before doing this, `web` shows `Restarting`
> in `docker compose ps` and its log carries the `cannot access path …/out` line. Run the
> bootstrap above, then `up -d web` — no rebuild, no redeploy needed.

### Step 2 — migrate, seed the registry, verify

    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
      traffic-service migrate
    # expected: applied 0001_init.sql … applied 0004_daily_aggregates.sql, then "migrations applied: 4"

    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
      traffic-service seed-zones
    # expected: "zones upserted: 95 (floor_version=p20-v2)"

    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
      traffic-service doctor
    # expected exactly, in this order:
    #   migrations: 4
    #   zones: 95
    #   published: 89
    #   published zones missing K: 0
    #   last slot: <YYYY-MM-DD>T<HH>-00 <status>   <- printed only once a capture has run
    #                                                (a fresh volume has no slots row yet)
    # exit 0 (a non-zero exit prints UNHEALTHY to stderr)

`doctor` must print **95 / 89 / 0** — 95 zones seeded, 89 published, 0 published zones
missing their capacity — and, once any capture has run, the newest `slots` row as
`last slot: <key> <status>`. That is the gate for "the registry is correct".

### Step 3 — start the whole stack

    docker compose -f deploy/compose.prod.yml --env-file <env> up -d --wait
    docker compose -f deploy/compose.prod.yml --env-file <env> ps

Expected: `db` and `web` `running`/`healthy`, and `worker` `running` — `worker` declares no
`HEALTHCHECK`, so `ps` never reports it as `healthy` (the one-shot `run --rm` containers from
above show as `Exited`). Then confirm the site is up (**before any publish it looks empty on
purpose**):

    docker compose -f deploy/compose.prod.yml --env-file <env> exec -T web \
      node -e "fetch('http://127.0.0.1:3000/en').then(r=>{console.log(r.status)})"

Expected: `200`.

> **The site is only as fresh as the last publish.** Before the first `night` succeeds,
> `/data/*.csv` and `manifest.json` return **200 with EMPTY bodies** — `web-entrypoint.sh`
> creates each missing registered data file as an empty one at start-up so a fresh volume
> cannot 500. **An empty-looking site before the first publish is expected, not a bug.** And
> until the frontend freshness change (plan 5) ships the `manifest.json`-driven re-fetch, a
> returning visitor keeps the dataset they first loaded (see §11, "stale-looking site").

---

## 7. Acceptance on the VPS

The order below is the spec's deploy acceptance (spec:234-245): a real 12-slot day captured,
one night batch published, the site fresh over HTTPS, and one deliberately killed capture
slot producing an alert.

### 7a. A real 12-slot day with the FULL registry

There is a deploy-time check the local smoke does **not** cover: the local smoke proves the
DB → frame → publish path on a **3-zone subset**, and the full path has never run at **all 89
published zones**. Run at least one real day end to end at the full registry:

1. Let the scheduler capture a full day (or run the 12 slots by hand). A real slot with the
   full registry is the capture-side proof — the CLI prints its counts on **separate lines**,
   and the zone count is **89** (the registry's `publish = true` set), never 95:

       docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
         traffic-service capture --slot now
       # expected, in this order (`slot` is the even-hour slot the clock is in; `dir` is that
       # key under --raw-root):
       #   slot: 2026-10-12T14-00
       #   status: ok
       #   captured: 89
       #   failed: 0
       #   skipped: 0
       #   dir: /data/raw/2026-10-12T14-00
       # exit 0 when the status is ok; any other status exits 1

2. After the day closes, the `night` batch must publish. Confirm the day's twelve `slots`
   rows are complete first:

       docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
         sh -c 'psql "$DATABASE_URL" -c "select count(*) as slot_rows, sum(captured) as captured, sum(expected) as expected from slots where slot_ts >= now() - make_interval(days => 1)"'
       # expected: slot_rows 12; captured/expected >= 0.95 (the publish gate)
       # (make_interval(days => 1), not interval '1 day': a single quote inside the
       #  single-quoted sh -c argument cannot be escaped, and this form needs none)

3. **Verify the night from both routes.** The scheduled 00:30 job has no console of its own,
   so `scheduler.run_night_job` logs the run's summary as **one line**; the `job_runs` row is
   the audit record. Both, then the authoritative one:

       # route 1 — the scheduled job's own line (`logs worker` holds the current container's
       # output only, so a much older night can have rotated away):
       docker compose -f deploy/compose.prod.yml --env-file <env> logs worker | grep -F 'night ' | tail -3
       # expected, one line per night run — the same fields `night` prints, on one line:
       #   2026-10-13 00:30:02,345 INFO traffic_service.scheduler: night 2026-10-12: images: 1068  measured: 1068  skipped: 0  errors: 0  slots: 12  unknown: 0  areas: 89  cities: 27  days: 1  error_rows: 0  data_version: 2026-10-12-<12 hex>  pruned: 12 slot(s), <N> byte(s)
       # (a manual `traffic-service night --day <D> …` one-shot prints the same summary as
       #  separate lines to *its own* stdout instead — §11's backfill row shows that run.)

       # route 2 — AUTHORITATIVE: the night's row in the audit table:
       docker compose -f deploy/compose.prod.yml --env-file <env> exec -T db \
         psql -U traffic -d traffic -c "select job, target, ok, finished_at from job_runs where job = 'night' order by id desc limit 1"
       # expected (one row for the night the 00:30 job just ran):
       #    job  |  target    | ok |        finished_at
       #  -------+------------+----+------------------------
       #   night | 2026-10-12 | t  | 2026-10-13 00:34:11.482+01

   The **`job_runs` row is authoritative**: it is the record `alerts.health` reads, it
   survives a container recreate, and `ok`/`finished_at` are what an operator is asking
   about. The log line is that same run's detail (the counts and the `data_version` the site
   will serve) — same run, second witness. `exec -T db psql -U traffic -d traffic` is the
   §9 shape; substitute `POSTGRES_USER`/`POSTGRES_DB` if you changed them.

   The published **national index must land inside the 0.5–40 band** the historical range
   accepts:

       docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
         sh -c 'psql "$DATABASE_URL" -c "select day, round(national_ci::numeric,4) as national_ci, n_zones from index_daily order by day desc limit 1"'
       # expected: n_zones = 89, national_ci inside 0.5 … 40

   And the nine CSVs + `manifest.json` exist on the shared volume:

       docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
         sh -c 'ls -la /data/out'
       # expected: areas.csv cities.csv color_composition.csv daily.csv hourly.csv
       #           hourly_delta.csv monthly.csv road_classes.csv road_classes_hourly.csv
       #           manifest.json — all non-zero, manifest containing a fresh data_version

### 7b. The site serves the fresh data over HTTPS

From any machine, against the public hostname:

    curl -fsS  https://<host>/en        | head -c 200
    curl -fsS  https://<host>/data/manifest.json
    curl -fsS  https://<host>/data/daily.csv | head -1

Expected: `/en` returns the page (200); `manifest.json` returns 200 with a `data_version`
identical to the one the `night` run printed; `daily.csv` returns 200 with its header row and
real rows (not 0 bytes). The `data_version` being **the same string** the worker printed is
the publish → serve contract proven end to end.

### 7c. One deliberately killed capture slot produces an alert

Simulate the one silent failure the project most cares about — a slot that never completed —
so the alert path is proven. **First the ceiling on what this can show:** `alerts.health`
looks at the trailing 24 h of even-hour slots **excluding the slot containing `now`**, so a
slot you kill *right now* reports **nothing** until the clock crosses the next even boundary
(up to ~2 h later). Route A below makes the alert reachable straight away; Route B does it
without touching the DB but waits for that boundary. First, the honest immediate expectation:

    # kill the worker mid-slot, then run maintenance straight away:
    docker compose -f deploy/compose.prod.yml --env-file <env> stop worker
    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
      traffic-service maintenance
    # expected: NO slot warning yet. The slot you just killed is the one `health` deliberately
    # excludes (it may still be running); the next even hour moves it into the window, and the
    # next `maintenance` run (03:30, or a manual one) reports it from then on:
    #   warning: slot <key> still running (stale) — the worker died mid-slot
    # `maintenance --day <D>` does NOT shorten this: `--day` names only the `job_runs` row and
    # the dump file — `health` always reads the real clock. Re-running it sooner changes nothing.

Both routes need a capture from the last 24 h, so run this after §7a.

**Route A — deterministic, no wait.** Make a slot that is already ≥ 2 h old look like the one
the worker died in. List the window, flip the newest slot in it to `running`, run
`maintenance`, then put the status back:

    # 1. the last 24 h of slots, newest first — what `health` will look at:
    docker compose -f deploy/compose.prod.yml --env-file <env> exec -T db \
      psql -U traffic -d traffic -c "select slot_ts, status, captured, expected from slots where slot_ts >= now() - make_interval(hours => 24) order by slot_ts desc"

    # 2. the newest slot that is at least 2 h old — exactly the first slot `health` inspects:
    docker compose -f deploy/compose.prod.yml --env-file <env> exec -T db \
      psql -U traffic -d traffic -c "update slots set status = 'running' where slot_ts = (select max(slot_ts) from slots where slot_ts <= now() - make_interval(hours => 2))"

    # 3. run maintenance and confirm the health lens names it and the channel delivers it:
    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
      traffic-service maintenance
    # expected (stderr, and on your topic/phone with ALERT_WEBHOOK_URL set):
    #   warning: slot <key> still running (stale) — the worker died mid-slot

    # 4. put the status back (the value step 1 printed for that row — `ok` after §7a's slot):
    docker compose -f deploy/compose.prod.yml --env-file <env> exec -T db \
      psql -U traffic -d traffic -c "update slots set status = 'ok' where slot_ts = (select max(slot_ts) from slots where slot_ts <= now() - make_interval(hours => 2))"

**Route B — no DB edit.** Leave the worker stopped: every even-hour slot that fires while it
is down has no row at all, and the first one to fall ≥ 2 h behind the clock (the next even
boundary, ≤ 2 h) is reported as

    # with the worker still stopped, at or after the next even hour:
    #   warning: no slot row for <key> — the capture never ran

Then start the worker again (`docker compose … up -d worker`). A missed slot stays missed —
this route simulates a **real** hole, not a status flag — so `no slot row for <key>` keeps
firing for the next 24 h: fill that slot with
`… run --rm worker traffic-service capture --slot <ts>` (§11's stuck-slot row) before the next
`night`, or the day fails the twelve-slot publish gate. Route A leaves nothing behind once its
step 4 has put the status back. Both warnings are exactly what §8's alert list names — a stale
`running` slot, and a slot that never ran.

### 7d. (Local, disposable) the fixture smoke

`deploy/make_fixture_day.py` is the committed acceptance tool: it lays a corpus day out as
`/data/raw/<slot>/` so a `night` can be reproduced from the repo alone, and its `--print-sql`
mode emits the trim plus the day's twelve `slots` rows. Two commands:

    python deploy/make_fixture_day.py
    python deploy/make_fixture_day.py --print-sql | \
      docker compose -f deploy/compose.prod.yml --env-file <env> run --rm -T worker \
        sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1'

> **`--print-sql` emits DESTRUCTIVE SQL.** It deletes `measurements`, `captures`, `slots`,
> the three rollup tables, and **every zone it does not name**. Run it against a **disposable
> LOCAL database only.** Never against the VPS database: it will delete the production
> registry and the entire measurement history.

---

## 8. Alerts

One channel: `ALERT_WEBHOOK_URL`. It is POSTed a plain-text body, which is exactly what ntfy
consumes (`curl -d "msg" https://ntfy.sh/<topic>`); a Telegram relay or a Coolify webhook can
be adapted to it. Empty = alerts are **logged and dropped**, never an exception.

1. Create an **ntfy topic** (e.g. `https://ntfy.sh/traffic-index-<random>`); for Telegram,
   point ntfy's topic at a bot chat (an ntfy `X-Click`/`X-Action` or an ntfy→Telegram bridge).
2. Set `ALERT_WEBHOOK_URL` to the topic URL in the Coolify env and redeploy.
3. Verify:

       docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
         sh -c 'python3 -c "from traffic_service.alerts import notify; print(notify(\"deploy check\"))"'
       # expected: True, and the message arrives on the topic.

**What fires** (`alerts.health` + `alerts.disk_usage`, collected by `maintenance` and sent
after its `job_runs` row is written):

- a slot with **no row** — "the capture never ran";
- a slot **stuck `running`** (stale) — "the worker died mid-slot";
- a slot captured **short** (`partial` / `failed` / `overrun`);
- the **last `night` run failed** — reported when it did;
- **disk > 80 %** on the raw volume.

The window is the trailing **24 h** of even-hour slots, **excluding** the slot containing
`now` (it may legitimately still be running).

**The night abort is CLI-only — know the chain.** When a `night` publish aborts, the
`night --day` CLI itself calls the webhook (`night <day> aborted: …`), but an **automated**
abort on the scheduled path does **not** call the webhook directly. The failure surfaces on
the **03:30 `maintenance`** row: `alerts.health` reads the **last `night` `job_runs` row** and
reports `night <target> failed: <error>`. So look at the **maintenance** health lens (and its
webhook) for a failed scheduled night — not at the night job's own output. One consequence for
§7a: the one-line summary the scheduled job logs is logged **only by a run that reached its
publish**, so a missing `night <day>:` line beside an `ok = false` `job_runs` row is an abort,
not a lost log line — the traceback is in `logs worker` under the scheduler's own logger.

---

## 9. Backups and restore

`maintenance` (03:30, and any manual run) runs **prune → dump → vacuum → health**: the nightly
`pg_dump` into `/data/dump` is the second step, ahead of every read that could fail (the
`VACUUM`, the slot-health query, the disk check), so the backup is never skipped by a later
error. Retention is today's dump plus the **14** newest older ones (`keep=14` → up to 15
files). A failed `pg_dump` raises and the run's `job_runs` row reads `ok = false` with the
reason.

    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm worker \
      sh -c 'ls -la /data/dump && gzip -t /data/dump/$(date +%F).sql.gz && echo "gzip OK"'
    # expected: 2026-…sql.gz present; "gzip OK"

**Restore drill** — do this once to prove the dump restores. The dump lives in the
**worker's** `/data/dump`, so stream it out of the worker into the `db` container:

    # 1. stream the dump from the worker's volume into psql (the db container reads stdin):
    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm -T worker \
        sh -c 'gunzip -c /data/dump/<YYYY-MM-DD>.sql.gz' \
      | docker compose -f deploy/compose.prod.yml --env-file <env> exec -T db \
        psql -U traffic -d traffic -v ON_ERROR_STOP=1

    #    expected: no errors; ends with CREATE / COPY / ALTER output.

    # 2. sanity-check the restored data:
    docker compose -f deploy/compose.prod.yml --env-file <env> exec -T db \
      psql -U traffic -d traffic -c 'select count(*) from zones; select count(*) from measurements;'

> Do the drill against a **fresh, disposable** database, never over the live one — step 1
> writes into whatever `psql -d traffic` names. To restore onto a genuine fresh volume, first
> `migrate` that empty database (§6 Step 2), then run step 1. The brief's shortened form
> (`… exec -T db psql -U traffic -d traffic < <(gunzip -c <file>)`) works the same way once
> the dump file is on the host — copy it out first:

    docker compose -f deploy/compose.prod.yml --env-file <env> run --rm -T worker \
      cat /data/dump/<YYYY-MM-DD>.sql.gz > restore.sql.gz

> **Offsite copy (spec open item 3, the user's decision).** `/data/dump` lives on the same
> VPS, so it does **not** survive a disk loss. Copy the nightly `.sql.gz` off the box — a
> Coolify S3 target, the school NAS, `rsync`/`scp` to another host, or "nowhere" if the user
> accepts the loss. The runbook names where it lands; it does not configure it.

---

## 10. Upgrades

1. Build and push a new tag on the dev box (§3):

       tag="$(date +%Y%m%d)-$(git rev-parse --short HEAD)"
       bash deploy/build-worker.sh "$tag" --push
       bash deploy/build-web.sh    "$tag" --push

   (Rebuild only the image that changed — the pins are independent.)
2. In the Coolify env, change **`TRAFFIC_WORKER_IMAGE`** and/or **`TRAFFIC_WEB_IMAGE`** to the
   new tag.
3. **Redeploy** the resource. Coolify pulls the new image and recreates the container.

The worker resumes from DB state: every job is **idempotent by unique key** — a re-run of a
slot overwrites the same PNG and re-upserts the same `captures` rows, the night's rollups are
written `on conflict … do update`, and the night's own `slots` upsert is `on conflict … do
nothing` (a real capture's counts are never clobbered by the night pass). A `night` re-run of
an already-published day reproduces the same content-addressed `data_version`. An interrupted
job simply re-runs on the next fire (the advisory locks make a duplicate a no-op, recorded
`ok = true` with `metrics {"skipped": "locked"}`). No data migration step is needed between
releases unless a migration file was added, in which case run `migrate` (§6 Step 2) after the
redeploy.

Rollback is the same procedure with the previous tag: set the pins back and redeploy.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `web` is `Restarting`; log shows `cannot access path …/_data/out: no such file or directory` | fresh volume: the `out/` subpath does not exist yet (§6 Step 1) | run the bootstrap one-liner `… run --rm worker sh -c 'mkdir -p /data/out /data/raw /data/dump'`, then `up -d web` |
| Site loads but every dataset is empty (200, 0 bytes) | no publish yet — the entrypoint's placeholders are empty by design | expected before the first `night`; a successful publish fills them (§7a) |
| Site shows old data after a publish | browser ETag revalidation, or the plan-5 re-fetch not shipped | see the two rows below |
| Returning visitor keeps their first-loaded dataset | plan 5's `manifest.json`-driven re-fetch is not in the frontend yet | known until plan 5 ships; the `data_version` in `manifest.json` is the intended cache-bust |
| A retry still serves a stale CSV | `ETag` / `Last-Modified` on the data files are **frozen at image-build time** (`ETag: "0-…"`) and do **not** change when the volume changes, so a browser that revalidates by ETag can serve the stale file | restart/reconfigure the frontend around the `data_version` cache-bust, or verify with `curl -H 'Cache-Control: no-cache'`; the served bytes are current (`size: 0` means nitro streams the mounted file whole) |
| Added/renamed a published CSV and it 404s/307s | nitro **freezes the served asset map at build time** (that is why the data files are zero-byte placeholders in the image) | a **new CSV name requires rebuilding image B** (§3): it is a code + rebuild change, not a volume change |
| `capture` fails: `Executable doesn't exist at …` | the image lost its Chromium | **rebuild the worker image** with `deploy/build-worker.sh <tag> --push` — never `pip install` inside a running container |
| `PipelineNotFound` | the image is missing `data engineering/` | rebuild from `deploy/build-worker.sh` (the staging copies the five files the pipeline imports) |
| Error contains a malformed `DATABASE_URL` / `could not parse` | the password contains `@ : / # ?` (not URL-safe) | change `POSTGRES_PASSWORD` to a URL-safe value and redeploy; `DATABASE_URL` interpolates the password straight into a URL |
| A slot is stuck `running` | the worker died mid-slot | the `maintenance` health check names it; re-run the slot with `traffic-service capture --slot <YYYY-MM-DDTHH:00>` |
| A `night` day never published | the day failed validation (capture ratio, a zero-coverage zone, or a non-finite index) | read the stderr reasons; fix and **re-run within 48 h** — see the two rows below |
| A `night` was missed and the day has not published | **measurement happens ONLY in the `night` job** (the scheduler's catch-up covers capture only) | re-run within **48 h**: `… run --rm worker traffic-service night --day <D> --raw-root /data/raw --out-dir /data/out --workers 4`. After 48 h the prune has taken the pixels and the day is unrecoverable from PNGs |
| Backfilling a day **older than 48 h** | `maintenance` prunes on the 48 h slot clock while holding only its own lock (a consciously accepted cross-job race) | **stop the scheduler first** (`docker compose … stop worker`), run the `night --day <D>` backfill, then start it again — a concurrent 03:30 pass can delete the pixels mid-measure |
| The `night` row looks fine but the prune did not happen | the night `job_runs` row does **not** carry the prune outcome (the prune is opportunistic and runs outside the audit block) | a failed prune surfaces on the **03:30 `maintenance`** row (§8): read that row's problems |
| The VPS cannot reach GHCR | network policy on the school machine | ship the images instead of pulling them: `docker save ghcr.io/tak1edd1ne/traffic-worker:<tag> \| gzip > worker.tgz` on the dev box, `scp worker.tgz <vps>:`, `gunzip -c worker.tgz \| docker load` on the VPS — repeat for **both** images, then **keep the same image pins** in the Coolify env (they now resolve to the loaded images) |
| Fresh deploy: `db` healthy but nothing captures | `worker` has not reached the top of the hour yet, or its `DATABASE_URL` is wrong | check `logs worker`; confirm `doctor` connects (§6 Step 2) |

> Note on the web container's data placeholders: they are created **root-owned**. That is
> harmless today because the `worker` also runs as **root**, but if a future change drops the
> worker to a non-root user, the worker will not be able to overwrite those placeholder files
> on the shared volume — revisit ownership at that point.
