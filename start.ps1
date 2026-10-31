param([switch]$Mock, [switch]$NoBrowser, [int]$Port = 0)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ and select Add Python to PATH.' }
}
& $pythonPath -c 'import mcp, fastapi, uvicorn, httpx, jinja2, dotenv'
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network and restart.' }
}
if ($Mock) { $env:FISHWORLD_PROVIDER = 'mock' }
if ($Port -gt 0) { $env:FISHWORLD_PORT = "$Port" }
& $pythonPath app.py $(if ($NoBrowser) { '--no-browser' })
