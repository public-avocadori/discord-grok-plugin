# discord-grok-plugin — container image
FROM python:3.12-slim

# Persist short-term memory under a mountable volume.
ENV DISCORD_STATE_DIR=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown -R appuser /data
USER appuser

VOLUME ["/data"]

# Provide configuration via -e/--env-file at runtime:
#   docker run --env-file .env -v dgp-data:/data ghcr.io/<you>/discord-grok-plugin
CMD ["discord-grok-plugin"]
