# Образ RAG из лабы 1. Один образ, две роли:
#   rag-indexer   - разовая задача: скачать документацию Odoo, обработать, построить индекс (index.sh)
#   rag-assistant - постоянный сервис: отвечает на вопросы (uvicorn, API из lab1/src/api/app.py)
# Код не копируется в lab2, а берётся из lab1: контекст сборки = корень репозитория.
#   docker build -f lab2/services/rag-assistant/Dockerfile -t pad/rag .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# git нужен grabber-у лабы 1 (sparse clone репозитория odoo/documentation)
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY lab1/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
COPY lab1/run.py ./run.py
COPY lab1/configs ./configs
COPY lab1/src ./src
COPY lab2/services/rag-assistant/index.sh ./index.sh

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app /data
USER 10001

# DATA_DIR - общий том: indexer пишет туда индекс и модель MiniLM, assistant читает.
ENV SERVICE_NAME=rag-assistant DATA_DIR=/data API_HOST=0.0.0.0 API_PORT=8000 LOG_FORMAT=json READY_REQUIRES_LLM=false
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["python", "run.py", "serve"]
