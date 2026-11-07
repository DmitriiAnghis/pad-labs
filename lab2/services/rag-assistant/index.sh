#!/bin/sh
# rag-indexer: построить индекс в DATA_DIR. Повторный запуск дешёвый: grabber скачивает только
# изменённые файлы (blob SHA), обработка и индекс пересобираются только при изменениях.
# В конце пишет файл-метку DATA_DIR/.index-ready: в Kubernetes rag-assistant ждёт её
# в initContainer и стартует только когда индекс уже готов (лаба 3).
set -e
DATA_DIR="${DATA_DIR:-/data}"
rm -f "$DATA_DIR/.index-ready"
python run.py models download minilm
python run.py grab
python run.py process
python run.py index
python run.py status
date -u +%Y-%m-%dT%H:%M:%SZ > "$DATA_DIR/.index-ready"
