FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the local reranker model into the image so it never downloads at runtime.
ENV FLASHRANK_CACHE=/opt/flashrank
RUN python -c "from flashrank import Ranker; Ranker(model_name='ms-marco-TinyBERT-L-2-v2', cache_dir='/opt/flashrank')" \
 && chmod -R a+rX /opt/flashrank

COPY cypress ./cypress
COPY migrations ./migrations

RUN useradd --uid 10001 --no-create-home app
USER app

# API by default; the ingest Cloud Run Job overrides the command with
#   python -m cypress.ingest.worker --once
CMD ["sh", "-c", "exec uvicorn cypress.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
