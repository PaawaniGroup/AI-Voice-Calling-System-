# Qwen3-ASR-1.7B RunPod Serverless worker. Weights are downloaded at worker init into HF_HOME (no weights, no secrets in image).
# Build: docker build -t mmeog/mme-qwen3-asr:1.0.0 .
FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/models/hf QWEN_ASR_REQUIRE_CUDA=1 QWEN_ASR_REQUIRE_PINNED=1
RUN useradd -m -u 10001 worker && mkdir -p /models/hf && chown -R worker /models
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY handler.py ./
COPY app ./app
COPY config ./config
USER worker
# Weights are fetched on first start (HF_TOKEN not required: the model is public). See README for cold-start trade-offs.
CMD ["python", "-u", "handler.py"]
