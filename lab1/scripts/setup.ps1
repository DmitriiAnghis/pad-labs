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

# Ollama must be running: the LLM and the multilingual embeddings of the default index live there
$envVars = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object { $envVars[$Matches[1]] = $Matches[2].Trim() }
$ollama = if ($envVars["OLLAMA_URL"]) { $envVars["OLLAMA_URL"] } else { "http://localhost:11434" }
try { Invoke-RestMethod "$ollama/api/tags" -TimeoutSec 5 | Out-Null }
catch { Write-Error "Ollama is not reachable at $ollama. Install it from https://ollama.com, start it, or fix OLLAMA_URL in .env"; exit 1 }
foreach ($m in @($envVars["LLM_MODEL"], $envVars["EMBED_MODEL"]) | Where-Object { $_ } | Select-Object -Unique) {
    Write-Host "== ollama pull $m"; ollama pull $m
}

Write-Host "== downloading the MiniLM ONNX model for the MaxSim reranker (~90 MB)"
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
