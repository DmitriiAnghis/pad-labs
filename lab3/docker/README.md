# Docker: образы сервисов лабы 2

Здесь описано, где лежат Dockerfile-ы, как они устроены и как каждое требование лабы 3 к
Dockerfile выполнено. Сами Dockerfile-ы лежат рядом с кодом сервисов в `lab2/`, потому что
лаба 3 упаковывает приложение лабы 2, а не копирует его.

Термины:
* **образ (image)** — архив с файловой системой: Python, библиотеки, код сервиса и команда запуска.
  Из одного образа можно запустить сколько угодно контейнеров.
* **контейнер** — запущенный процесс из образа, изолированный от остальной системы.
* **слой (layer)** — каждая строка `RUN`, `COPY` в Dockerfile даёт слой. Docker запоминает
  слои и при повторной сборке берёт неизменённые из кэша.
* **контекст сборки** — папка, которую `docker build` отправляет сборщику; `COPY` берёт файлы
  только из неё.

## 1. Какие образы есть

| Образ | Dockerfile | Контекст сборки | Что внутри |
|---|---|---|---|
| `pad/auth-service:1.0.0` | `lab2/services/auth-service/Dockerfile` | `lab2/` | пакет `pad_common` + код auth-service |
| `pad/catalog-service:1.0.0` | `lab2/services/catalog-service/Dockerfile` | `lab2/` | то же для catalog |
| `pad/inventory-service:1.0.0` | `lab2/services/inventory-service/Dockerfile` | `lab2/` | то же для inventory |
| `pad/order-service:1.0.0` | `lab2/services/order-service/Dockerfile` | `lab2/` | то же для order |
| `pad/fulfillment-service:1.0.0` | `lab2/services/fulfillment-service/Dockerfile` | `lab2/` | то же для fulfillment |
| `pad/notification-service:1.0.0` | `lab2/services/notification-service/Dockerfile` | `lab2/` | то же для notification |
| `pad/gateway:1.0.0` | `lab2/gateway/Dockerfile` | `lab2/` | `pad_common` + gateway |
| `pad/rag:1.0.0` | `lab2/services/rag-assistant/Dockerfile` | корень репозитория | код лабы 1 (`lab1/src`, `run.py`, `configs`) + git |

Контекст `lab2/`, а не папка сервиса, потому что каждому сервису нужен общий пакет
`lab2/libs/pad_common` (логи, ошибки, `/health`, `/ready`, JWT, события). Образ RAG собирается
из корня, чтобы взять код лабы 1 без копирования в лабу 2. Образ `pad/rag` один на две роли:
`rag-indexer` (разовая задача, строит индекс) и `rag-assistant` (сервис ответов).

## 2. Как устроен Dockerfile сервиса (на примере order-service)

```dockerfile
FROM python:3.12-slim
```
Базовый образ: Debian без лишних пакетов + Python 3.12 (179 МБ на диске, 45 МБ при скачивании).

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
```
Python не пишет файлы `.pyc` (их негде хранить при read-only файловой системе в Kubernetes),
выводит логи сразу, без буфера (иначе `kubectl logs` показывал бы их с задержкой), pip не
хранит скачанные архивы внутри образа.

```dockerfile
WORKDIR /app
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
```
Рабочая папка и пользователь `app` с номером 10001 без права входа. Пользователь создаётся до
копирования кода, поэтому этот слой не пересобирается при правке кода.

```dockerfile
COPY libs/pad_common /tmp/pad_common
RUN pip install /tmp/pad_common && rm -rf /tmp/pad_common
COPY services/order-service/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
COPY services/order-service/order_service ./order_service
```
Порядок слоёв от редко меняющихся к часто меняющимся: сначала зависимости, потом код.
Правка кода сервиса пересобирает только последний `COPY`.

```dockerfile
USER 10001
ENV SERVICE_NAME=order-service PORT=8000
EXPOSE 8000
HEALTHCHECK ... /health
CMD ["sh", "-c", "exec uvicorn order_service.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
```
Процесс запускается от `app`, а не от root. `exec` заменяет оболочку `sh` на uvicorn (веб-сервер
для FastAPI), поэтому сигнал остановки от Docker или Kubernetes (SIGTERM) получает сам сервис и
корректно закрывает соединения. `HEALTHCHECK` используется docker compose; Kubernetes его
игнорирует и использует свои пробы из манифестов.

## 3. Требования лабы 3 к Dockerfile и как они выполнены

| Требование | Как сделано | Чем проверено |
|---|---|---|
| Небольшой образ | `python:3.12-slim`, нет компиляторов (все библиотеки ставятся готовыми wheel-архивами), `PIP_NO_CACHE_DIR`, у RAG после `apt-get` удаляется `/var/lib/apt/lists` | образ сервиса 302 МБ на диске, 71 МБ при скачивании; из них 179 МБ — базовый образ, 97 МБ — общие зависимости (FastAPI, SQLAlchemy, psycopg, aio-pika). Эти два слоя одинаковы у всех 7 образов и хранятся на диске один раз |
| `.dockerignore` | у каждого Dockerfile свой `Dockerfile.dockerignore` рядом с ним: в контекст попадают только `libs/pad_common` и папка этого сервиса, без `tests`, `.env`, `__pycache__`. Плюс общий `lab2/.dockerignore` для старых версий Docker | `docker run --rm pad/order-service:1.0.0 ls -R /app` показывает только `order_service/` и `requirements.txt` |
| Кэширование слоёв | порядок: база → пользователь → `pad_common` → `requirements.txt` → код | правка `order_service/__init__.py`: повторная сборка 0,5 с, 5 слоёв из кэша; правка кода *другого* сервиса: 0,36 с, все слои из кэша (контекст order-service не изменился) |
| Нет секретов в образе | пароли и ключ JWT приходят только переменными окружения (compose: `lab2/.env`, Kubernetes: Secret). `.env` исключён из контекста | `docker history pad/order-service:1.0.0` — нет ни одной строки с паролем; в контексте нет `.env` |
| Не root | `USER 10001`; в Kubernetes дополнительно `runAsNonRoot: true` | `docker run --rm pad/gateway:1.0.0 id` → `uid=10001(app)` |
| Правильный CMD/ENTRYPOINT | `CMD` в exec-форме, внутри `exec uvicorn`, порт из `PORT` | `kubectl delete pod` notification-service: за 1 с в логе `service stopped` и `Application shutdown complete` (корректное завершение), а не принудительное убийство через 20 с (`terminationGracePeriodSeconds`) |

## 4. Почему так, а не иначе

| Вариант | Плюсы | Минусы | Решение |
|---|---|---|---|
| `python:3.12-slim` (выбран) | маленький, есть `sh` для отладки, glibc: все wheel-архивы ставятся без компиляции | 179 МБ | выбран |
| `python:3.12-alpine` | база около 50 МБ | библиотека musl вместо glibc: часть пакетов (psycopg, bcrypt, onnxruntime для RAG) собирается из исходников или не ставится, сборка дольше | нет |
| distroless (образ без оболочки) | ещё меньше, нет `sh` для атакующего | нельзя зайти внутрь (`kubectl exec ... sh`) на защите и при отладке | нет, для учебного проекта отладка важнее |
| multi-stage (сборка в одном образе, запуск в другом) | выигрыш, когда нужен компилятор | здесь компилятор не нужен: все пакеты готовые, выигрыш почти ноль | нет |
| один общий образ на все сервисы | одна сборка | любая правка пересобирает и перезапускает всё, сервисы не независимы | нет: требование лабы — Dockerfile на сервис |

## 5. Сборка

Обычный Docker (для docker compose):
```powershell
cd lab2
docker compose build            # все 8 образов с тегом 1.0.0 (переменная IMAGE_TAG меняет тег)
```

Для Minikube (образы сразу внутри кластера): `lab3/scripts/build-images.ps1`, см. `lab3/README.md`.
