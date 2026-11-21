# syntax=docker/dockerfile:1
FROM python:3.14.10-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/
RUN python -m venv /venv && /venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.14.10-slim
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1
RUN useradd --create-home app
WORKDIR /app
COPY --from=deps /venv /venv
COPY --chown=app:app app ./app
USER app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-graceful-shutdown", "10"]
