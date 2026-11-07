<#
Собрать все 8 образов лабы 2 сразу внутри Minikube.

Берёт: Dockerfile-ы из lab2 (gateway, 6 сервисов, rag) и docker-compose.yml лабы 2, где у каждого
       образа есть имя и тег (pad/order-service:1.0.0 и т.д.).
Делает: по умолчанию (-Mode docker-env) переключает команду docker этого окна PowerShell на Docker
        внутри Minikube и собирает образы там: Kubernetes находит их сразу, без реестра.
        -Mode load: собирает образы в обычном Docker Desktop, затем копирует в Minikube
        (minikube image load). Медленнее, но работает, если docker-env по какой-то причине не подходит.
Получается: образы pad/*:1.0.0 в Minikube (проверка: minikube image ls | findstr pad/).
Запуск из корня репозитория:
    powershell -ExecutionPolicy Bypass -File lab3\scripts\build-images.ps1
    powershell -ExecutionPolicy Bypass -File lab3\scripts\build-images.ps1 -Mode load
#>
param(
    [ValidateSet("docker-env", "load")] [string]$Mode = "docker-env",
    [string]$Tag = "1.0.0"
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$compose = Join-Path $repo "lab2\docker-compose.yml"
$images = "auth-service", "catalog-service", "inventory-service", "order-service",
          "fulfillment-service", "notification-service", "gateway", "rag"
$env:IMAGE_TAG = $Tag

# docker compose читает lab2/.env (пароли) даже для сборки; если его нет, создаём шаблон
if (-not (Test-Path (Join-Path $repo "lab2\.env"))) {
    Write-Host "lab2\.env not found: run python lab2\scripts\init_env.py first" -ForegroundColor Yellow
    exit 1
}

if ($Mode -eq "docker-env") {
    Write-Host "== docker -> Docker inside Minikube"
    & minikube -p minikube docker-env --shell powershell | Invoke-Expression
}

Write-Host "== building images with tag $Tag"
# сборка через compose: имена, теги и контексты сборки уже описаны в lab2/docker-compose.yml
docker compose -f $compose build auth-service catalog-service inventory-service order-service `
    fulfillment-service notification-service gateway rag-indexer
if ($LASTEXITCODE -ne 0) { throw "docker compose build failed" }

if ($Mode -eq "load") {
    foreach ($name in $images) {
        Write-Host "== minikube image load pad/${name}:$Tag"
        minikube image load "pad/${name}:$Tag"
        if ($LASTEXITCODE -ne 0) { throw "minikube image load failed for $name" }
    }
}

Write-Host "== images in Minikube:"
minikube image ls | Select-String "pad/"
