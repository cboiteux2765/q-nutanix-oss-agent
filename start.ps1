$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
foreach ($dependency in @('uv', 'npm', 'git')) {
    if (-not (Get-Command $dependency -ErrorAction SilentlyContinue)) {
        throw "Install $dependency and run this script again."
    }
}
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw 'Python dependency setup failed.' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend/node_modules'))) {
    npm --prefix frontend ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency setup failed.' }
}
npm --prefix frontend run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
uv run evo
