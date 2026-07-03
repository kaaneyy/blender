# Headless Blender worker. Build from the repo root:
#   docker build -f blender/Dockerfile .
# Ubuntu's blender package is headless-capable; pin a specific Blender
# release via the official tarball if reproducible geometry matters.
FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends blender ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY schemas /app/schemas
COPY standards /app/standards
COPY blender /app/blender
COPY examples /app/examples

# The Redis-driven job loop lands in Phase 5 (T5.1). Until then this image is
# used interactively, e.g.:
#   docker compose run blender-worker \
#     blender -b -P blender/build_cli.py -- examples/street_light.json /exports/light.dae
CMD ["tail", "-f", "/dev/null"]
