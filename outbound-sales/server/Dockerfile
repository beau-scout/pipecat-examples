FROM dailyco/pipecat-base:latest

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Uncomment if you wish to print a summary of the features available in the base image
# ENV PCC_LOG_FEATURES_SUMMARY=true

# Git is needed to install the pipecat-ai git dependency.
# TODO: remove once pipecat-ai 1.4.0 ships on PyPI and pyproject.toml points at it.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install the project's dependencies from pyproject.toml.
# NOTE: uv.lock is gitignored in this repo, so dependencies are resolved from
# pyproject.toml (the original used `uv sync --locked` with a lockfile).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --no-install-project --no-dev

# Copy the application code
COPY ./server_utils.py server_utils.py
COPY ./bot.py bot.py
