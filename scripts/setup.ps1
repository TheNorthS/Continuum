$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $projectRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination (Join-Path $projectRoot '.env')
}
Set-Location (Join-Path $projectRoot 'backend')
uv sync --extra dev
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run python -m app.cli init
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run python -m app.cli ingest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Set-Location (Join-Path $projectRoot 'frontend')
npm ci
exit $LASTEXITCODE
