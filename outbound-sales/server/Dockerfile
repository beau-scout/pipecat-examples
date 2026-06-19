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

# Install the project's dependencies.
# uv.lock is gitignored so we can't bind-mount it in the cloud build context;
# copy pyproject.toml and let uv resolve at build time instead.
COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

# Copy the application code
COPY ./server_utils.py server_utils.py
COPY ./bot.py bot.py