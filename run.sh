#!/bin/bash
# run.sh — Drop onto any machine. Builds image, creates conda env, runs ghost.
# No arguments. Just ./run.sh — it figures out what needs doing.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Image config ───────────────────────────────────────────────
# Bump the version number to force a rebuild.
IMAGE="ghost-daemon-3"
BASE_IMAGE="continuumio/miniconda3:24.7.1-0"

# ─── Conda env config ──────────────────────────────────────────
# Change the date suffix to force a rebuild.
CONDA_ENV="ghost-20260417"
CONDA_DIR="${HOME}/.conda/envs/${CONDA_ENV}"

# ─── Volume mounts ─────────────────────────────────────────────
# Bash array — comment/uncomment lines freely.
VOLUMES=(
    -v "${REPO_DIR}:/ghost"
    -v "${CONDA_DIR}:/opt/conda/envs/${CONDA_ENV}"
    -v "${HOME}/.env:/ghost/.env:ro"
    # -v "${HOME}/data:/data"
    # -v "${HOME}/.cache/huggingface:/root/.cache/huggingface"
)

# ─── GPU / NVIDIA config ───────────────────────────────────────
# Auto-detected. If nvidia-smi is available, GPU args are included.
# Otherwise skipped (allows running on Mac Mini or CPU-only hosts).
GPU_ARGS=()
if command -v nvidia-smi &>/dev/null; then
    GPU_ARGS=(
        --gpus all
        --runtime=nvidia
        -e NVIDIA_VISIBLE_DEVICES=all
        -e NVIDIA_DRIVER_CAPABILITIES=compute,utility
        # -e CUDA_VISIBLE_DEVICES=0,1
    )
fi

# ─── Entrypoint ────────────────────────────────────────────────
# Multi-line string. Runs inside the container.
# watchmedo auto-restarts the daemon whenever .py files change.
ENTRYPOINT="
source activate /opt/conda/envs/${CONDA_ENV} 2>/dev/null
export GHOST_HOME=/ghost
export PYTHONPATH=/ghost

pip install -q watchdog 2>/dev/null

exec watchmedo auto-restart \
    --directory=/ghost/ghost \
    --directory=/ghost/lib \
    --pattern='*.py' \
    --recursive \
    -- python -m ghost.daemon
"

# ─── Build commands (bash only) ────────────────────────────────
# Pure bash commands. Comments are stripped, lines joined with &&
# into a single RUN layer. No Dockerfile instructions here.
BUILD_CMDS="
# System packages
apt-get update
apt-get install -y --no-install-recommends git curl build-essential
# Node.js for opencode
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
# Cleanup
apt-get clean
rm -rf /var/lib/apt/lists/*
# opencode
curl -fsSL https://opencode.ai/install | bash
ln -sf /root/.opencode/bin/opencode /usr/local/bin/opencode
# Workspace
mkdir -p /ghost
"

# ═══════════════════════════════════════════════════════════════
# Stage 1: Build Docker image if it doesn't exist
# ═══════════════════════════════════════════════════════════════
if ! docker image inspect "${IMAGE}" &>/dev/null; then
    echo "→ Building image ${IMAGE}..."

    # Strip comments and blank lines, join with " && " into one RUN
    RUN_CMD=$(echo "${BUILD_CMDS}" | sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' | awk '{printf "%s%s", sep, $0; sep=" && "} END{print ""}')

    echo "FROM ${BASE_IMAGE}
RUN ${RUN_CMD}
WORKDIR /ghost
CMD [\"bash\"]" | docker build -t "${IMAGE}" -f - "${REPO_DIR}"

    echo "✓ Image ${IMAGE} built"
else
    echo "→ Image ${IMAGE} exists"
fi

# ═══════════════════════════════════════════════════════════════
# Stage 2: Create conda environment if it doesn't exist
# ═══════════════════════════════════════════════════════════════
if [ ! -d "${CONDA_DIR}" ] || [ ! -f "${CONDA_DIR}/bin/python" ]; then
    echo "→ Creating conda env ${CONDA_ENV}..."
    mkdir -p "$(dirname "${CONDA_DIR}")"

    docker run --rm \
        -v "$(dirname "${CONDA_DIR}"):/conda_envs" \
        -v "${REPO_DIR}:/ghost:ro" \
        "${IMAGE}" \
        bash -c "
            conda create -y -p /conda_envs/${CONDA_ENV} python=3.12 && \
            /conda_envs/${CONDA_ENV}/bin/pip install -r /ghost/requirements.txt && \
            /conda_envs/${CONDA_ENV}/bin/pip install watchdog
        "

    echo "✓ Conda env ${CONDA_ENV} created at ${CONDA_DIR}"
else
    echo "→ Conda env ${CONDA_ENV} exists"
fi

# ═══════════════════════════════════════════════════════════════
# Stage 3: Run the container
# ═══════════════════════════════════════════════════════════════
docker rm -f ghost-daemon 2>/dev/null || true

echo "→ Starting ghost daemon..."
docker run -d \
    --name ghost-daemon \
    --restart unless-stopped \
    "${VOLUMES[@]}" \
    "${GPU_ARGS[@]}" \
    -e GHOST_HOME=/ghost \
    -e PYTHONPATH=/ghost \
    "${IMAGE}" \
    bash -c "${ENTRYPOINT}"

echo "✓ Ghost daemon running"
echo "  Logs:  docker logs -f ghost-daemon"
echo "  Shell: docker exec -it ghost-daemon bash"
echo "  Stop:  docker stop ghost-daemon"
