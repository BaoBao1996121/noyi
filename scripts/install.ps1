$ErrorActionPreference = 'Stop'
$repoPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoPath
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Install Python 3.11+ first.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Install Node.js 22.19+ first.' }
python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
if ($LASTEXITCODE -ne 0) { throw 'Python version check failed.' }
node -e "const v=process.versions.node.split('.').map(Number);if(v[0]<22||(v[0]===22&&v[1]<19))process.exit(1)"
if ($LASTEXITCODE -ne 0) { throw 'Node.js 22.19+ required.' }
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) { python -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw 'Python environment setup failed.' }
& ./.venv/Scripts/python.exe -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& ./.venv/Scripts/python.exe -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw 'Application installation failed.' }
& npm.cmd ci --prefix ./src/personal_agent/pi_bridge --ignore-scripts
if ($LASTEXITCODE -ne 0) { throw 'Pi installation failed.' }
Write-Host 'Installation complete. Open Start Personal Agent.cmd.'
