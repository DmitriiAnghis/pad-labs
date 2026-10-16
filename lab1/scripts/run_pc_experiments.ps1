# Experiments that need the home PC: Ollama LLMs/embeddings and the cross-encoder reranker.
# The CPU-only experiments are already in experiments/results; this script adds the rows
# that were skipped in the cloud (results are merged by variant + language).
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_pc_experiments.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\run_pc_experiments.ps1 -SkipPull
param([switch]$SkipPull, [int]$Limit = 0)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".\.venv\Scripts\python.exe"

# read model tags from .env so the script and the pipeline use the same models
$envVars = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object {
    $envVars[$Matches[1]] = $Matches[2].Trim()
}
$ollama = if ($envVars["OLLAMA_URL"]) { $envVars["OLLAMA_URL"] } else { "http://localhost:11434" }
try { Invoke-RestMethod "$ollama/api/tags" -TimeoutSec 5 | Out-Null }
catch { Write-Error "Ollama is not reachable at $ollama. Start Ollama (ollama serve) or fix OLLAMA_URL in .env"; exit 1 }

if (-not $SkipPull) {
    $models = @($envVars["LLM_MODEL"], $envVars["LLM_MODEL_ALT"], $envVars["JUDGE_MODEL"], "bge-m3", "nomic-embed-text", "qwen3-embedding:0.6b") |
        Where-Object { $_ } | Select-Object -Unique
    foreach ($m in $models) { Write-Host "== ollama pull $m"; ollama pull $m }
}

$limitArgs = @()
if ($Limit -gt 0) { $limitArgs = @("--limit", "$Limit") }

# retrieval experiments: only the variants that were skipped without Ollama / sentence-transformers
& $py run.py experiment retrieval --only "dense-bge-m3,hybrid-bge-m3,dense-nomic,dense-qwen3-embedding" @limitArgs
& $py run.py experiment query_language --only "hybrid-minilm+translate,hybrid-bge-m3,hybrid-bge-m3+translate" @limitArgs
& $py run.py experiment reranker --only "cross_encoder,llm-pointwise" @limitArgs
# refusal threshold calibrated for bge-m3 (the MiniLM thresholds do not transfer)
& $py run.py experiment threshold_bge_m3 @limitArgs
# answers: two LLMs x two prompts (+ judge), then the context-size experiment
& $py run.py experiment generation @limitArgs
& $py run.py experiment context @limitArgs
& $py run.py report
Write-Host "Done: see experiments\REPORT.md"
