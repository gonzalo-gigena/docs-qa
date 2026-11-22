# syntax=docker/dockerfile:1
# Python 3.14, not 3.15 like the api: onnxruntime has no 3.15 wheels yet.
FROM python:3.14.10-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/
RUN python -m venv /venv && /venv/bin/pip install --only-binary=:all: -r /tmp/requirements.txt

FROM python:3.14.10-slim
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1
RUN useradd --create-home app
# The model, baked in. Same repo and revision as the tokenizer in apps/api/Dockerfile: change both together.
ARG MODEL=https://huggingface.co/qdrant/bge-small-en-v1.5-onnx-q/resolve/aa8f8b060edb00e03bfdd08813a2949946c8ba55
ADD --chmod=755 --checksum=sha256:51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431 ${MODEL}/model_optimized.onnx /models/bge-small/
ADD --chmod=755 --checksum=sha256:13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd ${MODEL}/config.json /models/bge-small/
ADD --chmod=755 --checksum=sha256:d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66 ${MODEL}/tokenizer.json /models/bge-small/
ADD --chmod=755 --checksum=sha256:0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53 ${MODEL}/tokenizer_config.json /models/bge-small/
ADD --chmod=755 --checksum=sha256:5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a ${MODEL}/special_tokens_map.json /models/bge-small/
WORKDIR /app
COPY --from=deps /venv /venv
COPY --chown=app:app app ./app
USER app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-graceful-shutdown", "25"]
