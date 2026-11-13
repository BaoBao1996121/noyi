# Noyi home install: always-on service, automatic upgrades from GitHub tags, hourly log upload.
#
# Run once on the home PC, in PowerShell opened with "Run as administrator":
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Safe to run again: existing data, keys and releases are kept.
#
#   -Root D:\Noyi         where code and data live (code and data in separate folders)
#   -OpsRepo <ssh url>    private log repository, default git@github.com:BaoBao1996121/noyi-ops.git
#   -KeepPowerPlan        do not change sleep settings
#   -DryRun               print every step and the scheduled task commands, change nothing
param(
  [string]$Root = 'D:\Noyi',
  [string]$Repo = 'https://github.com/BaoBao1996121/noyi.git',
  [string]$OpsRepo = 'git@github.com:BaoBao1996121/noyi-ops.git',
  [switch]$KeepPowerPlan,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Run([string]$what, [scriptblock]$action) {
  if ($DryRun) { Write-Host "[dry-run] $what"; return }
  & $action
  if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}
function Need($name, $wingetId) {
  if (Get-Command $name -ErrorAction SilentlyContinue) { return }
  Run "winget install $wingetId" { winget install --id $wingetId -e --silent --accept-package-agreements --accept-source-agreements }
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $DryRun) { throw 'Open PowerShell with "Run as administrator" and run this again (needed for the start-at-boot task).' }

Step '1/7 Tools: Git, Python 3.11+, Node.js 24'
Need git 'Git.Git'
Need python 'Python.Python.3.13'
Need node 'OpenJS.NodeJS.LTS'
if (-not $DryRun) {
  python -c "import sys; assert sys.version_info >= (3,11)"; if ($LASTEXITCODE) { throw 'Python 3.11+ required' }
  node -e "const v=process.versions.node.split('.').map(Number);process.exit(v[0]>22||(v[0]===22&&v[1]>=19)?0:1)"; if ($LASTEXITCODE) { throw 'Node.js 22.19+ required (24 recommended)' }
}
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
$pythonw = if ($python) { Join-Path (Split-Path $python) 'pythonw.exe' } else { 'pythonw.exe' }

Step "2/7 Folders under $Root and the code"
$repoDir = Join-Path $Root 'repo'; $bin = Join-Path $Root 'bin'; $keys = Join-Path $Root 'keys'; $ops = Join-Path $Root 'ops'
Run "create $Root\{repo,data,bin,keys}" { New-Item -ItemType Directory -Force -Path $Root, (Join-Path $Root 'data'), $bin, $keys | Out-Null }
if (-not (Test-Path (Join-Path $repoDir '.git'))) { Run "git clone $Repo" { git clone --quiet $Repo $repoDir } }
Run 'git fetch tags' { git -C $repoDir fetch --quiet --tags origin }
$latest = if ($DryRun -and -not (Test-Path $repoDir)) { 'home-YYYYMMDD-N' } else { (git -C $repoDir tag --list 'home-*' | Sort-Object { $p = $_ -split '-'; [int64]($p[1] + $p[2].PadLeft(3,'0')) } | Select-Object -Last 1) }
if (-not $latest) { throw 'No home-* release tag yet. Ask for one to be published, then run this again.' }
Write-Host "Newest release: $latest"
Run "copy home scripts from $latest into $bin" {
  # A zip file, not a pipe: Windows PowerShell 5 corrupts binary data piped between programs.
  $zip = Join-Path $Root 'home-scripts.zip'; $unpack = Join-Path $Root 'home-scripts'
  git -C $repoDir archive --format=zip -o $zip $latest scripts/home
  Expand-Archive -Force $zip $unpack
  Copy-Item -Force (Join-Path $unpack 'scripts\home\*') $bin
  Remove-Item -Recurse -Force $zip, $unpack
}

Step '3/7 DeepSeek key (stored as your user environment variable NOYI_DEEPSEEK_KEY, never in a file)'
if ([Environment]::GetEnvironmentVariable('NOYI_DEEPSEEK_KEY', 'User')) { Write-Host 'Already set; keeping it.' }
elseif (-not $DryRun) {
  $secure = Read-Host 'Paste your DeepSeek API key (input hidden)' -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  if (-not $plain.Trim()) { throw 'Empty key' }
  [Environment]::SetEnvironmentVariable('NOYI_DEEPSEEK_KEY', $plain.Trim(), 'User'); $plain = $null
} else { Write-Host '[dry-run] ask for the key and store it as a user environment variable' }

Step '4/7 Deploy key for noyi-ops (can write to that one repository only)'
$key = Join-Path $keys 'noyi-ops'
if (-not (Test-Path $key)) { Run 'ssh-keygen' { ssh-keygen -q -t ed25519 -N '""' -C 'noyi-home' -f $key } }
if (-not $DryRun) {
  Write-Host "`nAdd this public key to noyi-ops -> Settings -> Deploy keys, and tick 'Allow write access':" -ForegroundColor Yellow
  Get-Content "$key.pub"
  Start-Process ('https://github.com/' + ($OpsRepo -replace '^git@github.com:|\.git$','') + '/settings/keys/new')
  Read-Host 'Press Enter after you saved it'
}
$sshCmd = "ssh -i `"$($key -replace '\\','/')`" -o IdentitiesOnly=yes -o UserKnownHostsFile=`"$((Join-Path $keys 'known_hosts') -replace '\\','/')`" -o StrictHostKeyChecking=accept-new"
if (-not (Test-Path (Join-Path $ops '.git'))) { Run "git clone $OpsRepo" { $env:GIT_SSH_COMMAND = $sshCmd; git clone --quiet $OpsRepo $ops } }

Step "5/7 First release ($latest): install, rehearse, start"
$tasks = @(
  @{ Name = 'Service'; Args = "`"$bin\supervise.py`" --root `"$Root`""; Trigger = '/SC ONSTART' },
  @{ Name = 'Upgrade'; Args = "`"$bin\upgrade.py`" --root `"$Root`""; Trigger = '/SC MINUTE /MO 10' },
  @{ Name = 'OpsPush'; Args = "`"$bin\ops_push.py`" --root `"$Root`""; Trigger = '/SC HOURLY' }
)
$user = "$env:USERDOMAIN\$env:USERNAME"
foreach ($t in $tasks) {
  # S4U: runs whether or not you are logged in, without storing your Windows password.
  $cmd = "schtasks /Create /F /TN `"\Noyi\$($t.Name)`" /TR `"\`"$pythonw\`" $($t.Args -replace '"','\"')`" $($t.Trigger) /RU `"$user`" /NP /RL LIMITED"
  if ($DryRun) { Write-Host "[dry-run] $cmd" } else { cmd /c $cmd | Out-Null; if ($LASTEXITCODE) { throw "schtasks $($t.Name) failed" } }
}
Run 'start the service task' { schtasks /Run /TN '\Noyi\Service' | Out-Null }
Run "upgrade.py (first install of $latest)" { & $python (Join-Path $bin 'upgrade.py') --root $Root }

Step '6/7 Power: never sleep on AC power'
if ($KeepPowerPlan) { Write-Host 'Skipped (-KeepPowerPlan).' }
else { Run 'powercfg /change standby-timeout-ac 0' { powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0 } }
Write-Host 'To power on again after an outage, enable "Restore on AC power loss" (or similar) in the BIOS. A script cannot do that.'

Step '7/7 Self-check'
if ($DryRun) { Write-Host '[dry-run] check http://127.0.0.1:8765, git fetch, and one push to noyi-ops'; exit 0 }
$ok = $false
for ($i = 0; $i -lt 60 -and -not $ok; $i++) {
  try { $boot = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/bootstrap' -TimeoutSec 3; $ok = $boot.title -eq 'Noyi' } catch { Start-Sleep 2 }
}
if (-not $ok) { throw "Noyi did not answer on http://127.0.0.1:8765. See $Root\data\logs\server.log and $Root\upgrade.json" }
Write-Host 'Service: OK  http://127.0.0.1:8765'
git -C $repoDir fetch --quiet origin; if ($LASTEXITCODE) { throw 'git fetch from GitHub failed' }
Write-Host 'Upgrade check: OK'
& $python (Join-Path $bin 'ops_push.py') --root $Root; if ($LASTEXITCODE) { throw 'Could not push to noyi-ops; check the deploy key has write access' }
Write-Host 'Log upload: OK'
Write-Host "`nDone. Restart the PC once to confirm Noyi starts by itself. Then follow docs/home-deploy.md for the phone." -ForegroundColor Green
