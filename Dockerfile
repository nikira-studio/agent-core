# Agent Core's supported runtime is Python 3.11.
# Keep local development and compatibility checks on Python 3.11 unless this
# base image, requirements, and docs are updated together.
FROM --platform=$BUILDPLATFORM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY runner/ ./runner/
COPY templates/ ./templates/
COPY docs/ ./docs/
COPY .env.example ./

RUN mkdir -p /data && chown -R 1001:1001 /data

ENV PYTHONUNBUFFERED=1
ENV AGENT_CORE_PORT=3500
ENV AGENT_CORE_DATA_PATH=/data

EXPOSE 3500

USER 1001:1001

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${AGENT_CORE_PORT:-3500}"]
