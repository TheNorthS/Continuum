$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $projectRoot 'backend')
# Preserve optional model/Milvus dependencies already installed for this deployment.
uv run --inexact uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
exit $LASTEXITCODE
