<#
Развернуть всю систему в Minikube одной командой (после build-images.ps1).

Берёт: манифесты lab3\k8s (kustomization.yaml), пароли из lab2\.env.
Делает: 1) создаёт lab3\k8s\secrets\secrets.env (если его нет) из lab2\.env;
        2) kubectl apply -k lab3\k8s: namespace pad, ConfigMap, Secret, PostgreSQL, RabbitMQ,
           6 сервисов, gateway, web-ui, Ingress;
           с ключом -WithRag ещё kubectl apply -k lab3\k8s\rag: Job rag-indexer, rag-assistant;
        3) ждёт, пока база, брокер и все Deployment-ы станут готовыми.
Получается: работающая система в namespace pad. С -WithRag индексация RAG идёт дальше в фоне (Job).
Запуск из корня репозитория:
    powershell -ExecutionPolicy Bypass -File lab3\scripts\deploy.ps1
    powershell -ExecutionPolicy Bypass -File lab3\scripts\deploy.ps1 -WithRag
#>
param([switch]$WithRag)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repo

Write-Host "== secrets"
python lab3\scripts\make_secrets.py
if ($LASTEXITCODE -ne 0) { throw "make_secrets.py failed" }

Write-Host "== kubectl apply -k lab3\k8s"
kubectl apply -k lab3\k8s
if ($LASTEXITCODE -ne 0) { throw "kubectl apply failed" }
if ($WithRag) {
    Write-Host "== kubectl apply -k lab3\k8s\rag"
    kubectl apply -k lab3\k8s\rag
    if ($LASTEXITCODE -ne 0) { throw "kubectl apply (rag) failed" }
}

Write-Host "== waiting for PostgreSQL and RabbitMQ"
kubectl -n pad rollout status statefulset/postgres --timeout=180s
kubectl -n pad rollout status statefulset/rabbitmq --timeout=240s

Write-Host "== waiting for services"
foreach ($d in "auth-service", "catalog-service", "inventory-service", "order-service",
               "fulfillment-service", "notification-service", "gateway", "web-ui") {
    kubectl -n pad rollout status "deployment/$d" --timeout=180s
}

if ($WithRag) {
    Write-Host "== RAG index is built by Job rag-indexer (several minutes):"
    Write-Host "   kubectl -n pad logs -f job/rag-indexer"
}
Write-Host "== open the web UI in another window:"
Write-Host "   kubectl -n pad port-forward svc/web-ui 8090:8000"
Write-Host "   then open http://localhost:8090"
Write-Host "== open the gateway in another window:"
Write-Host "   kubectl -n pad port-forward svc/gateway 8080:8000"
Write-Host "   then: curl.exe http://localhost:8080/api/status"
kubectl -n pad get pods
