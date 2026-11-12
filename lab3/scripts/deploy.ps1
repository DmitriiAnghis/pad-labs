<#
Развернуть всю систему в Minikube одной командой (после build-images.ps1).

Берёт: манифесты lab3\k8s (kustomization.yaml), пароли из lab2\.env.
Делает: 1) создаёт lab3\k8s\secrets\secrets.env (если его нет) из lab2\.env;
        2) kubectl apply -k lab3\k8s: namespace pad, ConfigMap, Secret, PostgreSQL, RabbitMQ,
           7 сервисов, gateway, Job rag-indexer, rag-assistant, Ingress;
        3) ждёт, пока база, брокер и все Deployment-ы станут готовыми.
Получается: работающая система в namespace pad. Индексация RAG идёт дальше в фоне (Job).
Запуск из корня репозитория:
    powershell -ExecutionPolicy Bypass -File lab3\scripts\deploy.ps1
#>
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repo

Write-Host "== secrets"
python lab3\scripts\make_secrets.py
if ($LASTEXITCODE -ne 0) { throw "make_secrets.py failed" }

Write-Host "== kubectl apply -k lab3\k8s"
kubectl apply -k lab3\k8s
if ($LASTEXITCODE -ne 0) { throw "kubectl apply failed" }

Write-Host "== waiting for PostgreSQL and RabbitMQ"
kubectl -n pad rollout status statefulset/postgres --timeout=180s
kubectl -n pad rollout status statefulset/rabbitmq --timeout=240s

Write-Host "== waiting for services"
foreach ($d in "auth-service", "catalog-service", "inventory-service", "order-service",
               "fulfillment-service", "notification-service", "gateway") {
    kubectl -n pad rollout status "deployment/$d" --timeout=180s
}

Write-Host "== done. RAG index is built by Job rag-indexer (several minutes):"
Write-Host "   kubectl -n pad logs -f job/rag-indexer"
Write-Host "== open the gateway in another window:"
Write-Host "   kubectl -n pad port-forward svc/gateway 8080:8000"
Write-Host "   then: curl.exe http://localhost:8080/api/status"
kubectl -n pad get pods
