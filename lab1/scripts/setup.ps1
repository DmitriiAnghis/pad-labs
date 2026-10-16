# One-time setup on Windows (PowerShell), run from the lab1 folder:
#   cd pad-labs\lab1
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1          # core
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Extra   # + cross-encoder, Qdrant
param([switch]$Extra)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .venv)) {
    Write-Host "== creating virtual environment .venv"
    python -m venv .venv
}
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt
if ($Extra) { & $py -m pip install -r requirements-extra.txt }

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "== .env created from .env.example - check OLLAMA_URL and model tags (ollama list)"
}

Write-Host "== downloading the MiniLM ONNX embedding model (~90 MB)"
& $py run.py models download minilm
Write-Host "== grabbing Odoo documentation (git sparse checkout)"
& $py run.py grab
Write-Host "== preprocessing"
& $py run.py process
Write-Host "== building the default index"
& $py run.py index
Write-Host "== tests"
& $py -m pytest -q tests
& $py run.py status
Write-Host "Done. Try:  .\.venv\Scripts\python.exe run.py ask `"How do I enable wave transfers?`""
