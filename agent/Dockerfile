# Build from the REPO ROOT so persona.md is in the context:
#   docker build -f agent/Dockerfile -t jenny-agent .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ .
COPY persona.md /app/persona.md
ENV PERSONA_PATH=/app/persona.md

# Cloud Run provides $PORT.
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
