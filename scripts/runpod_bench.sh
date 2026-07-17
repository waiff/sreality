#!/usr/bin/env bash
# One-shot RunPod driver for the GPU embedding bake-off.
# Copy this + embedding_gpu_bench.py + the manifest to the pod, then:
#   bash runpod_bench.sh embedding_manifest.json [comma-separated HF model ids]
# HF weights + image cache + results land on /workspace (the network volume) so a
# stopped/restarted pod resumes warm.
set -euo pipefail
manifest="${1:?usage: runpod_bench.sh <manifest.json> [models]}"
models="${2:-facebook/dinov2-base,facebook/dinov2-large,facebook/dinov2-with-registers-large}"
export HF_HOME="${HF_HOME:-/workspace/hf}"
pip install -q transformers torchvision pillow requests
python3 "$(dirname "$0")/embedding_gpu_bench.py" \
  --manifest "$manifest" \
  --models "$models" \
  --cache-dir /workspace/imgcache \
  --out /workspace/results.json
echo "Results: /workspace/results.json"
