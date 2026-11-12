<#
Сценарий демонстрации на защите (раздел 14 требований), по шагам с паузами.

Берёт: развёрнутую систему (deploy.ps1) и открытый port-forward на gateway:
       kubectl -n pad port-forward svc/gateway 8080:8000   (в отдельном окне)
Делает: показывает Pod-ы, Service-ы, Deployment-ы, логи, реальные запросы к API, масштабирование
        и восстановление после удаления Pod-а. Перед каждым шагом ждёт Enter.
Запуск из корня репозитория:
    powershell -ExecutionPolicy Bypass -File lab3\scripts\demo.ps1
#>
$ErrorActionPreference = "Continue"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$base = "http://localhost:8080/api"
$adminPassword = (Select-String -Path (Join-Path $repo "lab3\k8s\secrets\secrets.env") -Pattern "^ADMIN_PASSWORD=(.*)$").Matches[0].Groups[1].Value

function Step($title) { Write-Host ""; Write-Host "=== $title" -ForegroundColor Cyan; Read-Host "Enter - выполнить" | Out-Null }

Step "1. Все Pod-ы (копии сервисов)"
kubectl -n pad get pods -o wide

Step "2. Все Service-ы (постоянные адреса)"
kubectl -n pad get services

Step "3. Deployment-ы и StatefulSet-ы (сколько копий хотим и сколько готово)"
kubectl -n pad get deployments,statefulsets,jobs

Step "4. Логи order-service (JSON, с request_id)"
kubectl -n pad logs deployment/order-service --tail=10

Step "5. Реальные запросы к API через gateway"
Invoke-RestMethod "$base/status" | ConvertTo-Json -Depth 5
$login = Invoke-RestMethod -Method Post "$base/auth/login" -ContentType "application/json" `
    -Body (@{ username = "admin"; password = $adminPassword } | ConvertTo-Json)
$h = @{ Authorization = "Bearer $($login.access_token)" }
$sku = "DEMO-" + (Get-Random -Maximum 99999)
$product = Invoke-RestMethod -Method Post "$base/products" -Headers $h -ContentType "application/json" `
    -Body (@{ sku = $sku; name = "Коробка для демонстрации" } | ConvertTo-Json)
Write-Host "created product $($product.id) $($product.sku)"
Invoke-RestMethod "$base/products?q=DEMO" -Headers $h | Select-Object -First 3 | Format-Table id, sku, name
Invoke-RestMethod -Method Post "$base/assistant/search" -Headers $h -ContentType "application/json" `
    -Body (@{ query = "removal strategy FEFO"; top_k = 3 } | ConvertTo-Json) |
    Select-Object -ExpandProperty results | Format-Table rank, title, url

Step "6. Масштабирование: order-service 2 -> 3 копии"
kubectl -n pad scale deployment order-service --replicas=3
kubectl -n pad rollout status deployment/order-service --timeout=120s
kubectl -n pad get pods -l app.kubernetes.io/name=order-service
kubectl -n pad get endpoints order-service

Step "7. Восстановление: удаляем один Pod catalog-service, Kubernetes создаёт новый"
$pod = kubectl -n pad get pod -l app.kubernetes.io/name=catalog-service -o jsonpath="{.items[0].metadata.name}"
kubectl -n pad delete pod $pod --wait=false
kubectl -n pad get pods -l app.kubernetes.io/name=catalog-service
Start-Sleep -Seconds 10
kubectl -n pad get pods -l app.kubernetes.io/name=catalog-service

Step "8. Вернуть order-service к 2 копиям"
kubectl -n pad scale deployment order-service --replicas=2
kubectl -n pad get deployments
