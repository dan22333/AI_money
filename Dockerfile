# Cloud Run container for the Jenny agent. Build from the REPO ROOT:
#   gcloud builds submit --tag <IMAGE> .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ .
# persona.md now lives in agent/, so it's already copied above.
ENV PERSONA_PATH=/app/persona.md

ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
