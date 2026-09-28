# Start the free local AI that Ask uses: Ollama and one small model, on THIS
# machine. Nothing is sent anywhere and nothing is billed.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_ai.ps1
#   $env:RADAR_LOCAL_MODEL = "qwen2.5:7b-instruct"; .\scripts\setup_ai.ps1   # bigger, better, slower
#
# Ask works without this: it answers from the board directly. The model adds
# free-form questions.
$ErrorActionPreference = "Stop"
$Model = if ($env:RADAR_LOCAL_MODEL) { $env:RADAR_LOCAL_MODEL.Trim() } else { "qwen2.5:3b-instruct" }
$HostUrl = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "http://localhost:11434" }

function Test-Ollama {
  try { Invoke-RestMethod -Uri "$HostUrl/api/tags" -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host "Installing Ollama with winget..."
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
  } else {
    Write-Host "Install Ollama from https://ollama.com/download (the Windows installer), then run this again."
    exit 1
  }
}

if (-not (Test-Ollama)) {
  Write-Host "Starting Ollama..."
  Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
  for ($i = 0; $i -lt 30 -and -not (Test-Ollama); $i++) { Start-Sleep -Seconds 1 }
  if (-not (Test-Ollama)) { Write-Host "Ollama did not start. Open the Ollama app once, then run this again."; exit 1 }
}

Write-Host "Downloading $Model (only the first time)..."
ollama pull $Model

Write-Host ""
Write-Host "Ready. Start the radar with this model, in the same window:"
Write-Host "  `$env:RADAR_LOCAL_MODEL = `"$Model`""
Write-Host "  `$env:RADAR_LLM_TIMEOUT = `"120`""
Write-Host "  uvicorn api.main:app --port 8000"
Write-Host ""
Write-Host "Then open Ask on the board: answers say 'written by $Model'."
