# PAD labs

Лабораторные работы по курсу PAD (UTM): RAG → микросервисы → Docker и Kubernetes.

| Лабораторная | Тема | Статус |
|---|---|---|
| [lab1](lab1/README.md) | RAG-система по документации Odoo 18 Inventory + Barcode | готова, эксперименты с LLM запускаются на ПК |
| [lab2](lab2/README.md) | складская система: 7 микросервисов + gateway + веб-интерфейс web-ui (MVC), RabbitMQ, PostgreSQL, RAG из lab1 как сервис | готова, запущена на ПК (без RAG): 55 unit/API + 5 e2e тестов |
| [lab3](lab3/README.md) | Docker-образы всех сервисов lab2 и развёртывание в Kubernetes (Minikube): Deployment, Service, ConfigMap, Secret, пробы, ресурсы, масштабирование | готова, запущена в Minikube на ПК (без RAG): e2e 5/5, Ingress, HPA |

Быстрый старт lab1 (Windows, PowerShell):

```powershell
cd lab1
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
.\.venv\Scripts\python.exe run.py ask "How do I enable wave transfers?"
```

Секреты и локальные настройки хранятся только в `lab1/.env` (в `.gitignore`); шаблон —
`lab1/.env.example`.
