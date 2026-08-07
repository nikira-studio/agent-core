# Agent Core's supported runtime is Python 3.11.
# Keep local development and compatibility checks on Python 3.11 unless this
# base image, requirements, and docs are updated together.
FROM --platform=$BUILDPLATFORM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY runner/ ./runner/
COPY docs/ ./docs/
COPY .env.example ./

RUN mkdir -p /data && chown -R 1001:1001 /data

ENV PYTHONUNBUFFERED=1
ENV AGENT_CORE_PORT=3500
ENV AGENT_CORE_DATA_PATH=/data

EXPOSE 3500

USER 1001:1001

# One worker by default, and that is the supported deployment. Several pieces of
# state are per-process — rate-limit buckets, the concurrent-search guard, and
# the dashboard's event stream — so a second worker silently multiplies the
# configured limits and leaves a browser connected to one process unable to see
# events published by another. Raising AGENT_CORE_WORKERS is possible and is the
# operator's call, but it is not a supported configuration today.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${AGENT_CORE_PORT:-3500} --workers ${AGENT_CORE_WORKERS:-1}"]
