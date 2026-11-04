param(
    [string[]]$TestArgs = @('-q')
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$pythonCommand = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { (Get-Command python).Source }
$tempRoot = Join-Path $repoRoot ('.pa\test-temp\' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$oldEnvironment = @{}
foreach ($name in @('TMP', 'TEMP', 'PYTHONPATH', 'PYTHONUTF8', 'PYTHONIOENCODING')) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$testExit = 1
Push-Location -LiteralPath $repoRoot
try {
    $env:TMP = $tempRoot
    $env:TEMP = $tempRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONPATH = Join-Path $repoRoot 'src'
    if ($oldEnvironment['PYTHONPATH']) {
        $env:PYTHONPATH += [IO.Path]::PathSeparator + $oldEnvironment['PYTHONPATH']
    }
    # A fresh child of this run's directory is exclusively owned by pytest.
    $pytestTemp = Join-Path $tempRoot 'pytest'
    & $pythonCommand -m pytest "--basetemp=$pytestTemp" @TestArgs
    $testExit = $LASTEXITCODE
}
finally {
    foreach ($name in $oldEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $oldEnvironment[$name], 'Process')
    }
    Pop-Location
}
exit $testExit
