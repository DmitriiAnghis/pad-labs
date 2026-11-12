# Образ auth-service. Собирается из папки lab2 (контекст сборки), чтобы взять общий пакет libs/pad_common:
#   docker build -f services/auth-service/Dockerfile -t pad/auth-service .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Слои от редко меняющихся к часто меняющимся: правка кода сервиса не переустанавливает зависимости.
COPY libs/pad_common /tmp/pad_common
RUN pip install /tmp/pad_common && rm -rf /tmp/pad_common
COPY services/auth-service/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
COPY services/auth-service/auth_service ./auth_service

# Запуск не от root: у пользователя app нет прав менять систему внутри контейнера.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

ENV SERVICE_NAME=auth-service PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=2)"
CMD ["sh", "-c", "exec uvicorn auth_service.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
