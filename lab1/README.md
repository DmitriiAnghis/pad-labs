# Лабораторная работа №1. RAG по документации Odoo Inventory

## 1. Описание проекта

RAG-система, которая отвечает на вопросы по документации складского модуля **Odoo 18
(Inventory + Barcode)** и указывает источники ответа. Если ответа в документации нет,
система говорит об этом, а не выдумывает.

* **Корпус:** официальная документация `odoo/documentation`, ветка `18.0`, разделы
  `inventory_and_mrp/inventory` и `inventory_and_mrp/barcode` (102 RST-файла, после очистки 94
  документа, 703 секции, ~560 тыс. символов).
* **Языки:** документы на английском; вопросы можно задавать на английском и русском.
* **Всё бесплатно и локально:** LLM и (опционально) embeddings через Ollama, остальное на CPU.
* **Главное в работе — исследование:** 47 тестовых вопросов, 14 наборов экспериментов, отчёт
  [experiments/REPORT.md](experiments/REPORT.md) и обоснование каждого решения в
  [DECISIONS.md](DECISIONS.md).

## 2. Архитектура

```text
 Офлайн (индексация)                                   Онлайн (вопрос)
 ───────────────────                                   ────────────────
 GitHub odoo/documentation                             вопрос (EN/RU)
        │ git sparse + shallow clone                      │ проверка, язык, [перевод]
        ▼                                                 ▼
 grabber ── manifest.json (blob SHA)                   Retriever: BM25 + dense ─► RRF → Top-20
        │ новые / изменённые / удалённые / дубли          │
        ▼                                                 ▼
 data/raw/files/*.rst                                  Filters: метаданные, длина, порог,
        │                                                 дубликаты, ≤3 chunk на документ
        ▼                                                 ▼
 preprocessing: RST → секции и блоки                   Reranker (опц.) → Top-5
        │ роли, таблицы, admonitions, ссылки              │
        ▼                                                 ├── пусто ─► отказ (LLM не вызывается)
 data/processed/docs.jsonl                                ▼
        │                                              LLM (Ollama, строгий промпт)
        ▼                                                 │ ответ + ссылки [n]
 chunking (fixed / overlap / paragraph / section)         ▼
        │ + metadata + контекстный заголовок            ответ + источники (title, section, URL)
        ▼
 embeddings (MiniLM-ONNX / TF-IDF / Ollama) ─► FAISS / Qdrant
 BM25 (свой, со стеммингом)
```

| Пакет `src/` | Что делает |
|---|---|
| `grabber/` | инкрементальный сбор: git sparse checkout, сравнение blob SHA с manifest, удаления, дубликаты, lock, журнал |
| `preprocessing/` | свой структурный RST-парсер, нормализация, 4 стратегии chunking, metadata |
| `embeddings/` | MiniLM-L6-v2 (ONNX, CPU), TF-IDF+LSA, любые модели Ollama; SQLite-кэш векторов |
| `vectorstore/` | FAISS (flat/HNSW) и Qdrant за одним интерфейсом |
| `retrieval/` | BM25, dense, hybrid (Reciprocal Rank Fusion), сборка/загрузка индекса |
| `filtering/` | фильтры контекста между retriever и LLM |
| `reranking/` | cross-encoder, LLM-reranker, bi-encoder, MaxSim (late interaction) |
| `generation/` | клиент Ollama, промпты, генератор с цитатами, extractive-baseline без LLM |
| `pipeline/` | онлайн-конвейер вопрос → ответ с таймингами каждого этапа |
| `evaluation/` | тестовый набор, метрики, эксперименты, отчёт |
| `api/` | REST API (FastAPI) + простая веб-страница |
| `common/` | настройки из `.env`, JSON-логи с request id, утилиты |

Структура `src/` соответствует требуемой; `vectorstore/`, `filtering/`, `pipeline/`, `api/` и
`common/` вынесены отдельно, потому что в лабораторных 2–3 это кандидаты в отдельные сервисы
(vector DB, API-шлюз) или общий код.

## 3. Используемые технологии

| Компонент | Выбор | Почему (подробно — DECISIONS.md) |
|---|---|---|
| Язык | Python 3.11+ | экосистема ML, FastAPI |
| Сбор | `git` (sparse, shallow, blob-less clone) | blob SHA = готовый детектор изменений, без API-лимитов GitHub |
| Парсинг | собственный RST-парсер | docutils/Sphinx падают на ролях Odoo и теряют структуру секций |
| Embeddings | all-MiniLM-L6-v2 (384, EN) через ONNX Runtime; TF-IDF+LSA (256); bge-m3 (1024, мультиязычная) через Ollama | MiniLM работает на любом CPU без PyTorch; bge-m3 нужна для русских вопросов |
| Векторное хранилище | FAISS `IndexFlatIP` | ~1 000 векторов: точный поиск < 1 мс; Qdrant поддержан для лаб 2–3 |
| Лексический поиск | свой BM25 + Snowball-стемминг | точные термины (SGTIN-96, FEFO, названия меню) |
| Fusion | Reciprocal Rank Fusion | не требует калибровки разных шкал score |
| Reranker | MaxSim на MiniLM (CPU), cross-encoder bge-reranker-v2-m3 (ПК) | см. эксперимент reranker |
| LLM | Qwen 3.5 (4B / 9B) через Ollama | бесплатно, локально, поддерживает русский |
| API | FastAPI + Uvicorn | Swagger из коробки, pydantic-валидация |
| Тесты | pytest | 47 тестов, без сети и без моделей |

## 4. Установка

Нужны Python 3.11+, git и (для генерации ответов) [Ollama](https://ollama.com).

```powershell
git clone https://github.com/DmitriiAnghis/pad-labs.git
cd pad-labs\lab1
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1          # venv, зависимости, модель, данные, индекс, тесты
# опционально: cross-encoder и Qdrant
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Extra
```

То же вручную:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env          # проверить OLLAMA_URL и имена моделей (ollama list)
python run.py models download minilm # ~90 МБ, один раз
python run.py grab                   # скачать / обновить документацию
python run.py process                # RST -> чистые структурированные документы
python run.py index                  # chunks + embeddings + FAISS + BM25
ollama pull qwen3.5:4b               # LLM для ответов
```

## 5. Запуск

```powershell
python run.py status                                   # настройки, данные, индексы, доступность Ollama
python run.py search "How do I enable wave transfers?" # только поиск, с оценками каждого кандидата
python run.py ask "Как включить волновые перемещения?" # полный ответ с источниками
python run.py serve                                    # API + веб-страница: http://127.0.0.1:8000
```

Ручная оценка ответов на тестовом наборе и проверка защиты от prompt injection:

```powershell
python run.py eval generation --lang ru                 # ответы на 47 вопросов -> data/runs/generation_ru_*.json
python run.py rate data\runs\generation_ru_<время>.json  # оценить каждый ответ: "4 incomplete # нет пути в меню"
python run.py ratings --save                            # сводка -> experiments/results/manual_ratings.json
python run.py experiment injection                      # 8 атак из data/eval/attacks.jsonl, промпты basic / strict_nodata / strict
```

Повторный `grab` скачивает только изменившиеся файлы, удалённые помечает `deleted`;
`process` и `index` пропускают работу, если исходные данные и конфигурация не изменились.

## 6. Configuration

Два уровня настроек:

* **`.env`** (переменные окружения, не попадает в Git) — где что лежит и какие модели:
  `OLLAMA_URL`, `LLM_MODEL`, `LLM_MODEL_ALT`, `JUDGE_MODEL`, `EMBED_MODEL`, `OLLAMA_TIMEOUT`,
  `LLM_TEMPERATURE`, `LLM_NUM_CTX`, `LLM_THINK`, `RERANK_MODEL`, `SOURCE_REPO_URL`,
  `SOURCE_BRANCH`, `DATA_DIR`, `RAG_CONFIG`, `API_HOST`, `API_PORT`, `LOG_LEVEL`,
  `LOG_FORMAT` (`text`/`json`), `HISTORY_ENABLED`. Шаблон с комментариями — [.env.example](.env.example).
  В лабораторных 2–3 это станет ConfigMap/Secret.
* **`configs/default.yaml`** — параметры конвейера (chunking, embeddings, retrieval, фильтры,
  reranker, генерация). Значения выбраны по результатам экспериментов. Любой параметр можно
  переопределить из командной строки:

```powershell
python run.py --set chunking.max_chars=500 --set retrieval.top_k=10 search "FEFO"
python run.py --set reranker.backend=cross_encoder ask "..."
python run.py --config configs\my.yaml ask "..."
```

Индексы хранятся в `data/indexes/<ключ конфигурации>/`, так что переключение между
конфигурациями не пересчитывает уже посчитанное; векторы кэшируются в SQLite.

## 7. API

`python run.py serve`, Swagger: `http://127.0.0.1:8000/docs`.

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | liveness: процесс жив (без проверки зависимостей) |
| GET | `/ready` | readiness: индекс загружен и Ollama доступна, иначе 503 |
| POST | `/ask` | ответ с источниками; `debug: true` добавляет все этапы поиска |
| POST | `/search` | только поиск: найденные, отброшенные (с причиной) и итоговые chunks |
| GET | `/history` | последние ответы `/ask` с их ручными оценками |
| POST | `/history/{answer_id}/rating` | ручная оценка ответа: `score` 1–5, `flags`, `comment` |
| GET | `/history/summary` | средняя оценка и доля каждого флага по модели/промпту |
| GET | `/config` | активная конфигурация и статистика индекса (без секретов) |
| GET | `/` | веб-страница для вопросов |

Каждый ответ содержит заголовок `X-Request-ID`; тот же id есть в каждой строке лога этого
запроса. Ошибки возвращаются как `{"error": "...", "request_id": "..."}` без stack trace:
422 — неверный вопрос, 503 — индекс ещё грузится, LLM или reranker недоступны (без тихой
подмены порядка), 500 — внутренняя ошибка.

Каждый ответ `/ask` получает `answer_id` и сохраняется в `data/history/answers.jsonl`
(в Git не попадает, отключается `HISTORY_ENABLED=false`); на веб-странице под ответом есть
форма оценки. Флаги: `factual_error`, `unsupported`, `incomplete`, `irrelevant`,
`wrong_refusal`, `bad_citation`.

## 8. Примеры использования

Запрос к API:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/ask -ContentType "application/json" `
  -Body '{"question": "How do I enable wave transfers?"}'
```

Ответ содержит поля `answer`, `refused`, `language`, `sources` (номер, название, секция, URL с
якорем, процитирован ли источник), `model` и `timings_ms` по этапам.

Настоящий вывод из облака, где нет Ollama, поэтому ответ собран extractive-генератором
(`--set generation.backend=extractive`). С LLM ответ будет связным текстом с теми же ссылками [n]:

```text
> python run.py --set generation.backend=extractive ask "How do I enable wave transfers?"
In the Operations section, tick the Batch, Wave & Cluster Transfers checkbox to enable the
setting. [1] This section appears only if Batch, Wave & Cluster Transfers is enabled in
Inventory app > Configuration > Settings. [2] To enable wave picking, begin by navigating to
Inventory > Configuration > Settings. [1]

Источники / Sources:
  [1] Wave transfers - Wave transfers > Configuration - https://www.odoo.com/documentation/18.0/applications/inventory_and_mrp/inventory/shipping_receiving/picking_methods/wave.html#configuration
  [2] Operation types - Operation types > Operation types > General tab > Batch & wave transfers - https://www.odoo.com/documentation/18.0/.../operation_type.html#batch-wave-transfers
  ...
refused=False model=extractive timings_ms={"embed_query": 0.3, "retrieve": 0.9, "filter": 2.2, "rerank": 411.2, "generate": 0.7, "total": 415.6}
```

Вопрос вне документации (настоящий вывод; отказ до вызова генератора, потому что включён порог
reranker 0.66, см. DECISIONS.md п. 9):

```text
> python run.py --set generation.backend=extractive --set reranker.min_score=0.66 ask "How do I configure payroll for warehouse employees?"
I don't have enough information in the documentation to answer this question.
refused=True reason=no_relevant_context model=none
```

С настройками по умолчанию (без порога) такой вопрос отклоняет LLM по строгому промпту.

## 9. Тестирование

```powershell
python -m pytest -q tests
```

47 тестов работают без сети, Ollama и скачанных моделей (используются фейковый git-источник,
фейковый LLM-клиент и TF-IDF): парсер RST, все стратегии chunking, BM25 и фильтры, метрики,
обнаружение изменений/удалений/дубликатов в grabber, отказы конвейера и ошибки Ollama,
контракт API, а также проверка, что каждая evidence-фраза тестового набора действительно есть
в документах.

Качество системы проверяется отдельно, на тестовом наборе:

```powershell
python run.py eval retrieval            # метрики поиска (EN)
python run.py eval retrieval --lang ru  # те же вопросы на русском
python run.py eval generation --judge   # ответы LLM + LLM-as-a-judge
python run.py experiment list           # все эксперименты
python run.py experiment chunking       # один эксперимент
python run.py report                    # experiments/REPORT.md
```

Эксперименты, которым нужна Ollama или cross-encoder, запускаются на ПК одной командой:
`powershell -ExecutionPolicy Bypass -File scripts\run_pc_experiments.ps1`.

## 10. Результаты экспериментов

Полные таблицы — [experiments/REPORT.md](experiments/REPORT.md), обоснование —
[DECISIONS.md](DECISIONS.md). 47 вопросов (39 с ответом + 8 без ответа), метрики по документам,
в скобках 95% bootstrap-интервал. Всё посчитано на CPU в облаке; строки с LLM, bge-m3 и
cross-encoder добавит `scripts\run_pc_experiments.ps1`.

| Эксперимент | Лучший / выбранный вариант | Ключевой результат |
|---|---|---|
| chunking (равный бюджет 4000 символов) | section-1000 (лучший: section-1500) | Evidence@ctx: fixed-1000 0.82, section-1000 0.87, section-1500 0.92; разница лучших в пределах шума, у 1500 17% chunks длиннее окна MiniLM |
| overlap | — (используется section) | fixed-1000: 0 → 0.82, 200 → 0.90, 300 → 0.92 |
| контекстный заголовок | включён | dense MRR 0.936 против 0.891 без заголовка |
| метод поиска (EN) | hybrid BM25 + MiniLM | MRR: BM25 0.861, TF-IDF 0.872, MiniLM 0.936, hybrid 0.932 [0.86–0.99] |
| метод поиска (RU) | нужен bge-m3 или перевод (ПК) | MRR 0.30–0.42 у всех CPU-вариантов |
| RRF | k = 60 | k = 10 → 0.938, 60 → 0.932: один вопрос разницы |
| Top-K для reranker | 20 | 5 → 0.974, 10/20/50 → 1.000; 50 вдвое медленнее и Evidence@5 ниже |
| reranker | MaxSim (MiniLM) | MRR 0.932 → **1.000**, Hit@1 0.897 → 1.000, +0.5 с |
| порог отказа | без порога | порог MaxSim 0.66 на EN: теряет 10% вопросов с ответом, отсекает 87.5% вопросов без ответа; на RU теряет все |
| векторное хранилище | FAISS flat | качество одинаковое; 2.7 / 2.8 / 5.0 мс (flat / HNSW / Qdrant) |
| генерация без LLM | baseline | extractive EN: правильное поведение 0.83, с порогом 0.89 |

Итоговый конвейер по умолчанию на английских вопросах: MRR **1.000**, Recall@5 0.966,
Evidence@5 0.872, поиск + rerank ~0.5 с на CPU.

## 11. Выводы

1. **Структура документа важнее размера chunk.** Нарезка по секциям и абзацам доносит до LLM
   нужную цитату чаще, чем окна фиксированной длины, при том же объёме контекста; overlap
   частично компенсирует разрезы, но раздувает индекс.
2. **Hybrid-поиск и контекстный заголовок — дешёвые и полезные приёмы.** Заголовок поднимает
   dense MRR на 0.045, BM25 добавляет точные термины.
3. **Reranker даёт самый большой прирост** (Hit@1 0.90 → 1.00) за ~0.5 с, что мало рядом со
   временем LLM.
4. **Главная нерешённая проблема — русские вопросы**: английская MiniLM и BM25 не сопоставляют
   русский вопрос с английским текстом (MRR ~0.4). Решение (bge-m3 или перевод вопроса)
   подготовлено и проверяется на ПК.
5. **Порог отказа нельзя выбрать один раз**: он зависит от модели и языка. Пока отказ
   обеспечивают строгий промпт и пустой контекст; порог калибруется после выбора модели.
6. **Ограничение оценки**: 39 вопросов с ответом, составленных по тексту документации; на
   английском лучшая конфигурация уже упирается в потолок (MRR 1.0), поэтому для дальнейших
   улучшений нужны более трудные, «пользовательские» вопросы.
7. Система готова к лабораторным 2–3 (health/readiness, request id, JSON-логи, конфигурация
   через окружение, Qdrant за интерфейсом); что придётся вынести в отдельные сервисы, перечислено
   в DECISIONS.md п. 13.
