#!/bin/bash
# test_docker.sh — Build ghost image and test opencode inside container.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${REPO_DIR}"

IMAGE="ghost-test-1"
BASE_IMAGE="continuumio/miniconda3:24.7.1-0"

echo ""
echo "═══ ghost Docker test ═══"
echo ""

# Wait for Docker daemon
echo "→ Waiting for Docker daemon..."
for i in $(seq 1 30); do
    if docker info &>/dev/null 2>&1; then
        echo "  Docker ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  ✗ Docker not ready after 60s"
        exit 1
    fi
    sleep 2
done

# ─── Stage 1: Build image ────────────────────────────────────
echo ""
echo "→ Building image ${IMAGE}..."

BUILD_CMDS="
apt-get update
apt-get install -y --no-install-recommends git curl build-essential
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
apt-get clean
rm -rf /var/lib/apt/lists/*
curl -fsSL https://opencode.ai/install | bash
ln -sf /root/.opencode/bin/opencode /usr/local/bin/opencode
mkdir -p /ghost
"

RUN_CMD=$(echo "${BUILD_CMDS}" | sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' | paste -sd'&&' - | sed 's/&&/ \&\& /g')

echo "FROM ${BASE_IMAGE}
RUN ${RUN_CMD}
WORKDIR /ghost
CMD [\"bash\"]" | docker build -t "${IMAGE}" -f - "${REPO_DIR}"

echo "  ✓ Image built"

# ─── Stage 2: Test opencode inside container ──────────────────
echo ""
echo "→ Testing opencode inside container..."

RESULT=$(docker run --rm "${IMAGE}" opencode version 2>&1) || true
echo "  opencode version: ${RESULT}"

if echo "${RESULT}" | grep -qiE '[0-9]+\.[0-9]+'; then
    echo "  ✓ opencode installed and working"
else
    echo "  ✗ opencode version check failed"
    echo "  Trying 'which opencode'..."
    docker run --rm "${IMAGE}" which opencode 2>&1 || echo "  not in PATH"
    docker run --rm "${IMAGE}" ls -la /root/.opencode/bin/ 2>&1 || echo "  no .opencode dir"
fi

# ─── Stage 3: Test ghost inside container ─────────────────────
echo ""
echo "→ Testing ghost modules inside container..."

docker run --rm \
    -v "${REPO_DIR}:/ghost:ro" \
    -e GHOST_HOME=/ghost \
    -e PYTHONPATH=/ghost \
    "${IMAGE}" \
    bash -c "
        pip install -q pyyaml 2>/dev/null
        python3 -c 'from ghost.config import GHOST_HOME; print(f\"  GHOST_HOME={GHOST_HOME}\")' && echo '  ✓ ghost.config'
        python3 -c 'from ghost.channels import write, read; print(\"  ✓ ghost.channels\")'
        python3 -c 'from ghost.agent_runtime import AgentRuntime; print(\"  ✓ ghost.agent_runtime\")'
        python3 -c 'from ghost.daemon import GhostDaemon; print(\"  ✓ ghost.daemon\")'
        python3 -c '
from ghost.config import load_config
cfg = load_config()
jobs = cfg.get(\"jobs\", [])
models = cfg.get(\"models\", {})
print(f\"  ✓ config.yaml: {len(jobs)} jobs, {len(models)} models\")
'
    "

# ─── Stage 4: Test opencode config generation ────────────────
echo ""
echo "→ Testing opencode config generation in container..."

docker run --rm \
    -v "${REPO_DIR}:/ghost:ro" \
    -e GHOST_HOME=/ghost \
    -e PYTHONPATH=/ghost \
    "${IMAGE}" \
    python3 -c "
import json, tempfile
from pathlib import Path
from ghost.agent_runtime import AgentRuntime
rt = AgentRuntime()
with tempfile.TemporaryDirectory() as tmp:
    ws = Path(tmp)
    rt._write_opencode_config(ws, {
        'provider': 'groq',
        'model': 'llama-3.3-70b-versatile',
        'api_key': 'test-key',
        'base_url': 'https://api.groq.com/openai/v1',
    })
    cfg = json.loads((ws / 'opencode.json').read_text())
    assert cfg['model'] == 'groq/llama-3.3-70b-versatile'
    assert cfg['providers']['groq']['apiKey'] == 'test-key'
    print('  ✓ opencode config generated correctly')
    print(f'  {json.dumps(cfg)}')
"

echo ""
echo "═══ Docker tests complete ═══"

# Cleanup
docker rmi "${IMAGE}" 2>/dev/null || true
