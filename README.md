# PAD labs

Лабораторные работы по курсу PAD (UTM): RAG → микросервисы → Docker и Kubernetes.

| Лабораторная | Тема | Статус |
|---|---|---|
| [lab1](lab1/README.md) | RAG-система по документации Odoo 18 Inventory + Barcode | готова, эксперименты с LLM запускаются на ПК |
| [lab2](lab2/README.md) | складская система: 7 микросервисов + gateway, RabbitMQ, PostgreSQL, RAG из lab1 как сервис | код и тесты готовы, RAG проверить на ПК |
| [lab3](lab3/README.md) | Docker-образы всех сервисов lab2 и развёртывание в Kubernetes (Minikube): Deployment, Service, ConfigMap, Secret, пробы, ресурсы, масштабирование | манифесты проверены в тестовом кластере, на ПК пройти шаги README |

Быстрый старт lab1 (Windows, PowerShell):

```powershell
cd lab1
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
.\.venv\Scripts\python.exe run.py ask "How do I enable wave transfers?"
```

Секреты и локальные настройки хранятся только в `lab1/.env` (в `.gitignore`); шаблон —
`lab1/.env.example`.
