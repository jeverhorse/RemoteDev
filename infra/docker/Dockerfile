# RemoteDev Linux Agent Environment Dockerfile
# Replicates Google Colab Linux Ubuntu x86_64 container environment
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install standard Linux development tools (matching Colab base utilities)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    bash \
    procps \
    ca-certificates \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install bridge dependencies
RUN pip install --no-cache-dir \
    aiohttp \
    watchfiles \
    pyyaml \
    psutil

# Prepare workspace directory
WORKDIR /workspace/project

# Create infra directory
RUN mkdir -p /infra /workspace/project

# Copy bridge package and config
COPY infra /infra

# Install win-run globally
RUN chmod +x /infra/bridge/win-run /infra/bridge/cli.py && \
    ln -sf /infra/bridge/win-run /usr/local/bin/win-run

ENV PYTHONPATH="/infra:$PYTHONPATH"
ENV REMOTEDEV_CONFIG="/infra/config/config.yaml"
ENV LINUX_WORKSPACE="/workspace/project"
ENV WINDOWS_HOST="host.docker.internal"

COPY infra/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
