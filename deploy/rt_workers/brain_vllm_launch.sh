#!/usr/bin/env bash
# Brain on an always-on host: vLLM's OpenAI-compatible server with PREFIX CACHING. NOT RUN, NOT built into any image (Stage 3 LLM gate decides).
# UNVERIFIED: the flag names below are those of the vLLM versions this project has used on the serverless worker (worker-vllm passes `vllm serve` flags as environment
# variables); check `vllm serve --help` of the version in the image before the first run. The model name and the weights are the tenant's current Brain (not named here).
#
# Why prefix caching: the system prompt (561 to 3,361 characters, about 140 to 1,680 tokens: ESTIMATE, counted in characters) is identical on every turn; with caching only
# the new turn is prefilled. The sliding history window (12 messages) invalidates the cache beyond the system prompt once it starts sliding (after turn 6).
set -euo pipefail
: "${MODEL:?set MODEL to the Brain model identifier}"
: "${VLLM_API_KEY:?set VLLM_API_KEY (the server refuses requests without it)}"
exec vllm serve "$MODEL" \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --api-key "$VLLM_API_KEY" \
  --enable-prefix-caching \
  --max-model-len "${MAX_MODEL_LEN:-4096}" \
  --max-num-seqs "${MAX_NUM_SEQS:-8}" \
  --gpu-memory-utilization "${GPU_MEM_UTIL:-0.90}"
# The service streams from <host>/v1/chat/completions (tenant config rt_transport.brain = "persistent", rt_endpoints.brain = the https root of this server).
