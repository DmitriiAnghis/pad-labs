#!/bin/sh
# rag-indexer: построить индекс в DATA_DIR. Повторный запуск дешёвый: grabber скачивает только
# изменённые файлы (blob SHA), обработка и индекс пересобираются только при изменениях.
set -e
python run.py models download minilm
python run.py grab
python run.py process
python run.py index
python run.py status
