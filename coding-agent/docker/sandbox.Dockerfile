# Sandbox image for the Coding Agent's Docker workspace backend.
# Runs with --network none, --cap-drop ALL and no secrets; everything the implementer or tests
# need must be baked in here. For reproducible pilots, pin the base image by digest after the
# first pull:  docker inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest==8.3.5"
WORKDIR /workspace
