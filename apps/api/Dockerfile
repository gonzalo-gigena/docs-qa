# syntax=docker/dockerfile:1
FROM python:3.15.2-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/
RUN python -m venv /venv && /venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.15.2-slim
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1
RUN useradd --create-home app
# The embedding model's own tokenizer, for chunk sizes. Same repo and revision as apps/embedder/Dockerfile.
ADD --chmod=755 --checksum=sha256:d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66 https://huggingface.co/qdrant/bge-small-en-v1.5-onnx-q/resolve/aa8f8b060edb00e03bfdd08813a2949946c8ba55/tokenizer.json /models/
WORKDIR /app
COPY --from=deps /venv /venv
COPY --chown=app:app app ./app
USER app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-graceful-shutdown", "10"]
