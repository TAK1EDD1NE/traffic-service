# syntax=docker/dockerfile:1.7
# Image A — capture + night worker.
#
# Base: the Playwright python image at the SAME version uv.lock resolves
# (playwright 1.63.0), so the Chromium build is baked in at build time and the
# school VPS never compiles or downloads a browser (spec:171-174).
#
# The install is EDITABLE on purpose: pipeline.py resolves `data engineering/` as
# `parents[3] / "data engineering"`, so the package has to stay in its source tree
# with that directory as a sibling. A wheel install would break every pipeline
# import and every capture with PipelineNotFound.
# Base pinned by digest. `mcr.microsoft.com/playwright/python:v1.63.0-noble` is a mutable tag, so a
# repush under it would silently swap the Chromium baked into a rebuild. The digest below is the one
# this image was built and smoke-tested against (recorded in task-2-build.log):
#   mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Africa/Algiers \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1

# pg_dump 16 for the nightly dump — a 14 client refuses a 16 server.
# If the noble repos do not carry postgresql-client-16, fall back to the newest
# client the image's own repos offer (never a 14): record what was installed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY traffic-service /app/traffic-service
COPY ["data-engineering", "/app/data engineering"]

# R28 — pin playwright to 1.63.0 in the SAME pip call as the extras: the extra
# alone declares `playwright>=1.54`, which lets pip resolve a NEWER library than
# the Chromium baked into this base image. A mismatched library refuses to drive
# that browser and `playwright install --with-deps chromium` would then download a
# second ~150 MB build at image-build time. Pinning keeps library == baked browser;
# the `playwright install` line stays as the safety net.
RUN python3 -m pip install --no-cache-dir -e "/app/traffic-service[browser,pipeline]" \
      "playwright==1.63.0" \
 && playwright install --with-deps chromium

VOLUME ["/data"]
CMD ["traffic-service", "scheduler"]
