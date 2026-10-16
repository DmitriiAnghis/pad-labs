# PAD labs

Лабораторные работы по курсу PAD (UTM): RAG → микросервисы → Docker и Kubernetes.

| Лабораторная | Тема | Статус |
|---|---|---|
| [lab1](lab1/README.md) | RAG-система по документации Odoo 18 Inventory + Barcode | готова, эксперименты с LLM запускаются на ПК |
| lab2 | микросервисный backend на основе RAG из lab1 | — |
| lab3 | контейнеризация и Kubernetes для lab2 | — |

Быстрый старт lab1 (Windows, PowerShell):

```powershell
cd lab1
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
.\.venv\Scripts\python.exe run.py ask "How do I enable wave transfers?"
```

Секреты и локальные настройки хранятся только в `lab1/.env` (в `.gitignore`); шаблон —
`lab1/.env.example`.
