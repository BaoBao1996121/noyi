# 家里电脑从零部署：逐条执行清单

按顺序做，每一步做完先核对「确认成功」再往下。原理（目录布局、升级怎么保证不坏库、回传了什么）见 [home-deploy.md](home-deploy.md)，这里不重复。

约定：

- 标【你手动】的步骤只能你本人在家里电脑或 iPhone 上完成。
- 下面的命令都在 PowerShell 里执行。要「以管理员身份运行」的会写明，其余用普通 PowerShell 窗口即可。
- 命令默认装在 `D:\Noyi`。安装时如果用 `-Root` 换了位置，下面所有 `D:\Noyi` 都要跟着换。
- 文档里不出现任何密钥值，只写环境变量名。
- 改 `D:\Noyi` 下的 JSON 文件只用本文给的命令。Windows PowerShell 5 的 `Set-Content -Encoding utf8` 会在文件开头写入 BOM，脚本读到这种文件会当成空设置，而且不会报错。

几个常用的查看命令，后面会反复用到：

```powershell
Get-Content D:\Noyi\supervisor.json -Encoding UTF8
```

```powershell
Get-Content D:\Noyi\current.json -Encoding UTF8
```

```powershell
Get-Content D:\Noyi\upgrade.json -Encoding UTF8
```

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/bootstrap | Select-Object title, version, remote_hosts
```

**重启服务（页面上没有停止按钮，用这条）**：它请求服务正常退出，退出前会保存执行状态；守护进程看到正常退出后，过约 1 秒用 `home.json` 的最新设置重新启动。

```powershell
$b = Invoke-RestMethod http://127.0.0.1:8765/api/bootstrap; Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/shutdown -Headers @{ 'X-PA-Token' = $b.token } -ContentType 'application/json' -Body '{}'
```

返回 `accepted : True` 就说明请求已被接受。约 10 秒后再看 `supervisor.json`，`started` 应变成刚才的时间，`state` 为 `running`。

---

## 0. 前置条件

**做什么**：逐项核对，缺什么先补上再开始。

1. Windows 10 22H2 或 Windows 11，并且有 `winget`（安装脚本靠它装 Git、Python、Node.js）。

   ```powershell
   winget --version
   ```

2. 你登录用的 Windows 账号是管理员。计划任务会以这个账号运行，DeepSeek 密钥也存在这个账号下，所以以后日常用的也应该是这个账号。

   ```powershell
   whoami /groups | Select-String 'S-1-5-32-544'
   ```

3. D 盘存在，剩余空间至少 10 GB（两个版本目录各带一套 Python 和 Node 依赖，另有账本备份）。

   ```powershell
   Get-PSDrive D | Select-Object Used, Free
   ```

4. 能连上 GitHub 的 HTTPS 和 SSH（22 端口），以及 DeepSeek。安装时还要从 PyPI 和 npm 下载依赖。

   ```powershell
   Test-NetConnection github.com -Port 22 | Select-Object TcpTestSucceeded
   ```

   ```powershell
   Test-NetConnection api.deepseek.com -Port 443 | Select-Object TcpTestSucceeded
   ```

   ```powershell
   Invoke-WebRequest https://pypi.org/simple/pip/ -UseBasicParsing -TimeoutSec 15 | Select-Object StatusCode
   ```

5. 系统自带 OpenSSH 客户端（生成部署密钥、推送日志要用）。

   ```powershell
   Get-Command ssh, ssh-keygen | Select-Object Source
   ```

6. 私有仓库 `noyi-ops` 已在 GitHub 上建好：浏览器登录 GitHub 后打开 <https://github.com/BaoBao1996121/noyi-ops> 能看到它。空仓库也可以。

7. 如果电脑上还没有真正的 Python：Windows 自带一个同名的「应用商店占位程序」，安装脚本会把它当成已装好的 Python，随后在版本检查处报 `Python 3.11+ required`。先检查：

   ```powershell
   Get-Command python -All | Select-Object Source
   ```

   结果只有 `...\WindowsApps\python.exe` 一行时，先装好 Python，**关掉这个窗口再开一个新的**：

   ```powershell
   winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements
   ```

**确认成功**：上面每条都有输出，`TcpTestSucceeded` 都是 `True`，`StatusCode` 是 `200`，`python --version` 显示 3.11 或更高。

**失败了怎么办**：22 端口不通时，换一个网络（比如手机热点）或者开代理后再试，日志回传只走 SSH；DeepSeek 或 PyPI 不通时，先把网络问题解决，安装第 5 步会下载依赖，网络不通就会失败。D 盘不够大时，安装时加 `-Root E:\Noyi` 换个盘，下文的路径都跟着换。
---

## 1. 可选：先预演一遍（-DryRun）

**做什么**：下载安装脚本，只打印每一步要做的事和三条建计划任务的命令，不改动任何东西。预演不需要管理员权限。

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/BaoBao1996121/noyi/main/scripts/home/install.ps1 -OutFile $env:TEMP\noyi-install.ps1 -UseBasicParsing
```

```powershell
powershell -ExecutionPolicy Bypass -File $env:TEMP\noyi-install.ps1 -DryRun
```

**确认成功**：依次打印 `== 1/7` 到 `== 7/7` 七段，中间有以 `[dry-run]` 开头的行，其中三行是 `schtasks /Create ... /TN "\Noyi\Service"`、`"\Noyi\Upgrade"`、`"\Noyi\OpsPush"`，最后一行是 `[dry-run] check http://127.0.0.1:8765, git fetch, and one push to noyi-ops`。从没装过时，`Newest release:` 后面显示占位符 `home-YYYYMMDD-N`，这是正常的。

**失败了怎么办**：第一条命令下载失败，说明连不上 GitHub，回到第 0 节的网络检查。预演没有改动任何东西，不需要回退。

---

## 2. 安装

**做什么**：右键「开始」→「终端（管理员）」或「Windows PowerShell（管理员）」，执行下面两条（第 1 节已下载过的话，第一条可以跳过）。全程约 30 分钟，大部分时间花在第 5 步下载依赖上。

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/BaoBao1996121/noyi/main/scripts/home/install.ps1 -OutFile $env:TEMP\noyi-install.ps1 -UseBasicParsing
```

```powershell
powershell -ExecutionPolicy Bypass -File $env:TEMP\noyi-install.ps1
```

脚本会依次做下面几件事，有两处要你动手：

1. `== 1/7`：没装的 Git、Python、Node.js 用 winget 装上。
2. `== 2/7`：建 `D:\Noyi` 下的目录，克隆公开仓库，打印 `Newest release: home-20261117-1`。
3. `== 3/7` 【你手动】：出现 `Paste your DeepSeek API key (input hidden)` 时，粘贴 DeepSeek 密钥后按回车。输入的内容不会显示。密钥存成你账号的环境变量 `NOYI_DEEPSEEK_KEY`。以前设过的话会显示 `Already set; keeping it.`，不再询问。
4. `== 4/7` 【你手动】：脚本打印一行以 `ssh-ed25519` 开头、以 `noyi-home` 结尾的公钥，并自动打开 `noyi-ops` 的「Add deploy key」页面。Title 随便填（比如 `noyi-home`），Key 栏粘贴这一整行，**勾选 Allow write access**，点 Add key。保存后回到 PowerShell 窗口按回车。
5. `== 5/7`：建三个计划任务，启动服务任务，然后首次安装版本。这里会打印 `upgrading (nothing) -> home-20261117-1`，安静地跑十几分钟（建虚拟环境、装依赖、跑冒烟测试、彩排），最后打印 `install`。
6. `== 6/7`：把「接通电源时从不睡眠、从不休眠」打开。不想改电源设置，第二条命令末尾加 `-KeepPowerPlan`。
7. `== 7/7`：自检，见第 3 节。

**确认成功**：最后一行是绿色的 `Done. Restart the PC once to confirm Noyi starts by itself.`。

**失败了怎么办**：

- 红字 `Open PowerShell with "Run as administrator"`：窗口不是管理员打开的，按上面的方法重开。
- 卡在 winget：手动装好报错的那个工具（`winget install --id Git.Git -e`，或者把 `Git.Git` 换成 `Python.Python.3.13`、`OpenJS.NodeJS.LTS`），**开一个新的管理员窗口**再运行第二条命令。
- `git clone` noyi-ops 失败：多半是部署公钥没保存，或者没勾 Allow write access。去 noyi-ops → Settings → Deploy keys 核对，然后重新运行第二条命令。
- 第 5 步打印的不是 `install`，而是 `rehearsal_failed` 或 `health_failed_rolled_back`：首次安装失败了，**直接重跑安装脚本不会再试这个版本**，按第 11 节「某个版本失败后不再重试」处理。
- 安装脚本可以放心重复运行：已有的数据、密钥、部署公钥和版本都会保留。要注意的只有一点：重跑会重建三个计划任务，第 4 节的时限修正要再做一次。
---

## 3. 自检

**做什么**：安装脚本最后自动做三项检查。装完后任何时候想复查，也可以手动跑下面几条。

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/bootstrap | Select-Object title, version
```

```powershell
Get-Content D:\Noyi\current.json -Encoding UTF8
```

```powershell
git -C D:\Noyi\releases\home-20261117-1 rev-parse --short HEAD
```

```powershell
python D:\Noyi\bin\ops_push.py --root D:\Noyi
```

**确认成功**：

- 安装窗口里依次出现 `Service: OK  http://127.0.0.1:8765`、`Upgrade check: OK`、`Log upload: OK`。三项都出现才算装好。
- 手动复查时：`title` 是 `Noyi`；`current.json` 里 `"tag": "home-20261117-1"`；版本目录的提交是 `adc3eda`；`ops_push.py` 打印 `pushed` 或 `no change`。
- 浏览器打开 <http://127.0.0.1:8765> 能看到 Noyi 页面。发一句话能收到回复，说明 DeepSeek 密钥可用。
- GitHub 上 noyi-ops 里出现 `latest.json` 和 `bundles/` 目录。

**失败了怎么办**：

- `Noyi did not answer on http://127.0.0.1:8765`：看 `D:\Noyi\upgrade.json` 的 `result` 和 `detail`，以及 `D:\Noyi\data\logs\server.log` 的最后几十行，对照第 11 节。
- `git fetch from GitHub failed`：连不上 GitHub，查网络后执行 `python D:\Noyi\bin\upgrade.py --root D:\Noyi` 重试。
- `Could not push to noyi-ops`：部署公钥没勾 Allow write access。在 GitHub 上删掉这个 key，用 `Get-Content D:\Noyi\keys\noyi-ops.pub` 取出公钥重新添加，勾上写权限，再执行上面的 `ops_push.py` 命令。
- 页面能打开但回复报错：密钥可能贴错了。在管理员窗口执行 `[Environment]::SetEnvironmentVariable('NOYI_DEEPSEEK_KEY', $null, 'User')` 清掉，重跑安装脚本重新粘贴，再按开头的「重启服务」命令重启一次。

---

## 4. 核对计划任务和 S4U，并修正运行时限

**做什么**：确认三个任务存在、以你的账号用 S4U 方式运行（不登录也会跑，也不保存 Windows 密码）。

```powershell
schtasks /Query /TN "\Noyi\Service"
```

```powershell
Get-ScheduledTask -TaskPath '\Noyi\' | Select-Object TaskName, State, @{n='LogonType';e={$_.Principal.LogonType}}, @{n='User';e={$_.Principal.UserId}}, @{n='TimeLimit';e={$_.Settings.ExecutionTimeLimit}}
```

```powershell
Get-ScheduledTask -TaskPath '\Noyi\' | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime
```

**运行时限和电池设置**：`schtasks /Create` 建出来的任务默认「运行超过 3 天就停止」（`TimeLimit` 显示 `PT72H`），还默认「用电池时不启动、切到电池就停止」。守护进程要一直跑。安装脚本（2026-11-17 起）建完任务会自动去掉这两项并核对，改不掉就直接报错。上面那条命令的 `TimeLimit` 列应显示 `PT0S`。如果显示的是 `PT72H`（比如用的是旧版脚本），在**管理员**窗口手动执行：

```powershell
foreach ($n in 'Service','Upgrade','OpsPush') { $t = Get-ScheduledTask -TaskPath '\Noyi\' -TaskName $n; $t.Settings.ExecutionTimeLimit = 'PT0S'; $t.Settings.DisallowStartIfOnBatteries = $false; $t.Settings.StopIfGoingOnBatteries = $false; Set-ScheduledTask -InputObject $t | Out-Null }
```

改的是任务设置，正在运行的守护进程不受影响，下次启动时生效。

**确认成功**：

- 列出 `Service`、`Upgrade`、`OpsPush` 三个任务，`LogonType` 都是 `S4U`，`User` 是你的账号。
- 修正后再跑第二条命令，`TimeLimit` 都是 `PT0S`。
- `Service` 的 `State` 是 `Running`，`LastTaskResult` 是 `267009`（表示正在运行）。`Upgrade` 和 `OpsPush` 跑过之后，`LastTaskResult` 是 `0`。

**失败了怎么办**：

- 少了某个任务，或者 `LogonType` 不是 `S4U`：在管理员窗口重跑安装脚本，它会用 `/F` 重新建任务，然后再做一次上面的修正。
- `Service` 不是 `Running`：执行 `schtasks /Run /TN "\Noyi\Service"`，10 秒后看 `D:\Noyi\supervisor.json` 的 `state`，含义见第 11 节。
---

## 5. 重启电脑，不登录，确认开机自启

**做什么**：

1. 【你手动】重启电脑，停在登录界面，**不要登录**，等 3 分钟。
2. 用同一局域网里的另一台设备确认服务起来了。服务只监听本机 127.0.0.1，局域网里直接访问不到，所以在 Tailscale 装好之前，只能用下面第 3 条间接确认。Tailscale 装好后（第 6 节），用手机打开 https 地址就是最直接的确认。
3. 登录后立刻执行下面两条，看守护进程的启动时间是不是早于你登录的时间：

```powershell
Get-Content D:\Noyi\supervisor.json -Encoding UTF8
```

```powershell
Get-ScheduledTask -TaskPath '\Noyi\' -TaskName Service | Get-ScheduledTaskInfo | Select-Object LastRunTime, LastTaskResult
```

4. 【你手动，可选】想停电恢复后自动开机：进 BIOS，打开「来电自动开机」（各厂商叫法不同，比如 Restore on AC Power Loss、AC Back、Power On after Power Failure），设为 Power On。脚本做不了这一步。

**确认成功**：`supervisor.json` 里 `state` 是 `running`，`started` 和 `LastRunTime` 都在开机那一分钟、早于你登录的时间。

**失败了怎么办**：

- `LastRunTime` 是你登录之后的时间，或者任务根本没跑：看「事件查看器 → 应用程序和服务日志 → Microsoft → Windows → TaskScheduler → Operational」里 `\Noyi\Service` 的记录。常见原因是第 4 节的修正没做，或者账号密码改过（S4U 不受改密码影响，但账号被禁用或改名会失效）。重跑安装脚本、再做一次第 4 节修正即可。
- `state` 是 `crash_loop` 或 `backoff`：服务起来又崩了，看 `D:\Noyi\data\logs\server.log` 最后几十行。开机后才出问题、登录后手动跑就正常的，多半是环境变量没读到；守护进程会从注册表补读 `NOYI_DEEPSEEK_KEY`，如果日志里提示缺少密钥，执行 `[Environment]::GetEnvironmentVariable('NOYI_DEEPSEEK_KEY','User') -ne $null`，结果应为 `True`。

---

## 6. Tailscale：让手机能访问

### 6.1 装 Tailscale 并登录【你手动】

**做什么**：家里电脑装 Tailscale 并登录；iPhone 从 App Store 装 Tailscale，登录**同一个账号**。国区 App Store 能不能搜到 Tailscale 还没核实，搜不到时需要用其他地区的 Apple ID。

```powershell
winget install --id Tailscale.Tailscale -e --accept-package-agreements --accept-source-agreements
```

装好后右下角托盘出现 Tailscale 图标，点它登录。然后**打开无人值守模式**：Windows 版 Tailscale 默认只在有人登录桌面时联网，重启后停在登录界面时手机就连不上。在普通 PowerShell 执行：

```powershell
tailscale set --unattended=true
```

**确认成功**：

```powershell
tailscale status
```

列表里能看到这台电脑和你的 iPhone。托盘图标右键 → Preferences 里 **Run unattended** 前面有勾。

**失败了怎么办**：`tailscale` 命令找不到，关掉窗口重开一个；还找不到就用完整路径 `& 'C:\Program Files\Tailscale\tailscale.exe' status`。

### 6.2 打开 MagicDNS 和 HTTPS 证书【你手动】

**做什么**：浏览器打开 <https://login.tailscale.com/admin/dns>，确认 **MagicDNS** 已开启；在同一页下方的 **HTTPS Certificates** 点 Enable。

**确认成功**：页面上方显示你的 tailnet 名字，形如 `tailxxxx.ts.net`；HTTPS Certificates 显示已启用。

**失败了怎么办**：没看到 HTTPS Certificates，就先开 MagicDNS，它依赖 MagicDNS。

### 6.3 执行 tailscale serve

**做什么**：在普通 PowerShell 执行。它把 tailnet 内的 https 地址转发到本机 8765 端口，只在你的 tailnet 里开放，不对公网开放，重启后依然有效。

```powershell
tailscale serve --bg 8765
```

**确认成功**：它打印一个地址，形如 `https://<电脑名>.tailxxxx.ts.net/`，记下其中的域名部分。之后随时可以用下面这条再看：

```powershell
tailscale serve status
```

**失败了怎么办**：提示要先启用 HTTPS 时，回到 6.2；想关掉转发执行 `tailscale serve --https=443 off`。

### 6.4 把域名写进 home.json，重启服务

**做什么**：把 6.3 记下的域名（只要域名，不带 `https://` 和末尾的 `/`）填进下面这条命令再执行。它会生成 `D:\Noyi\home.json`，用无 BOM 的 UTF-8 写入（原因见开头的约定）。不在这个名单里的地址一律拒绝访问。

```powershell
[IO.File]::WriteAllText('D:\Noyi\home.json', (@{ remote_hosts = @('noyi-home.tailxxxx.ts.net') } | ConvertTo-Json -Compress))
```

想再核对 Tailscale 带过来的登录身份，用这条代替上面那条（登录名就是 `tailscale status` 里显示的账号，通常是邮箱）：

```powershell
[IO.File]::WriteAllText('D:\Noyi\home.json', (@{ remote_hosts = @('noyi-home.tailxxxx.ts.net'); remote_login = 'you@example.com' } | ConvertTo-Json -Compress))
```

然后执行文档开头的「重启服务」命令。设置只在服务启动时读取，不重启不生效。

**确认成功**：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/bootstrap | Select-Object -ExpandProperty remote_hosts
```

打印出你的域名。iPhone 打开 Tailscale 连上后，用 Safari 访问那个 https 地址，能看到「配对这台设备」页面。

**失败了怎么办**：

- `remote_hosts` 是空的：多半是重启没生效，或者 `home.json` 写坏了。执行 `Get-Content D:\Noyi\home.json` 看内容，确认是一行合法的 JSON 且没有中文引号，然后再重启一次服务。
- 服务起不来，`server.log` 里有 `NOYI_REMOTE_HOSTS 只能是逗号分隔的域名`：域名填错了（多了 `https://` 或 `/`）。改正后重写 `home.json`，守护进程下次重启时会读到新设置。
- 想撤销：`Remove-Item D:\Noyi\home.json`，再重启服务，手机就访问不了了。
---

## 7. iPhone 配对和开启通知

### 7.1 生成配对码【你手动，家里电脑】

**做什么**：家里电脑打开 <http://127.0.0.1:8765>，点左下角 ⚙（设置），在弹窗下方「手机」一栏点「添加手机」，给手机起个名字，点「生成配对码」。

**确认成功**：弹窗里显示一个 `XXXX-XXXX` 形式的 8 位配对码和一个以 `https://<你的域名>/?pair=` 开头的链接。配对码 5 分钟内有效，只能用一次。

**失败了怎么办**：「生成配对码」按钮是灰的，并提示「还没配置外网地址」：第 6.4 节没生效，回去检查 `remote_hosts`。过期了就再点一次生成，新码会让旧码作废。

### 7.2 在 iPhone 上配对【你手动，iPhone】

**做什么**：iPhone 上打开 Tailscale 并保持连接，用 Safari 打开 `https://<你的域名>`，输入配对码，点「配对」。

**确认成功**：页面跳转到 Noyi 的对话页。回到家里电脑的「添加手机」弹窗，「已配对的设备」里出现这台手机。

**失败了怎么办**：

- Safari 打不开：iPhone 上的 Tailscale 没连上，或者 6.3 没执行。
- 显示「这个地址没有开放」：`home.json` 里的域名和 Safari 地址栏里的不一致。
- 显示「不是这台电脑主人的 Tailscale 账号」：`remote_login` 写错了，改正后重启服务，或者删掉这一项。
- 输错 5 次会锁 5 分钟，并作废当前配对码。等 5 分钟后重新生成。

### 7.3 添加到主屏幕并开启通知【你手动，iPhone】

**做什么**：

1. 在 Safari 里点底部的「分享」→「添加到主屏幕」。
2. **从主屏幕上的 Noyi 图标打开**（不要从 Safari 打开，否则不会出现开启通知的选项）。
3. 页面顶部出现「开启通知，每晚汇总和提醒会推到这台 iPhone」，点「开启」，在系统弹窗里选「允许」。

通知需要 iOS 16.4 或更高版本：「设置 → 通用 → 关于本机」里看 iOS 版本。

**确认成功**：页面提示「通知已开启」。iPhone「设置 → 通知」里出现 Noyi，并且是允许状态。

**失败了怎么办**：

- 顶部没出现开启通知的提示：确认是从主屏幕图标打开的；之前点过「以后再说」的话，提示不会再出现，删掉主屏幕图标，用 Safari 重新打开那个地址，再添加到主屏幕。
- 系统弹窗里选了「不允许」：到「设置 → 通知 → Noyi」里打开。
- 手机丢了，或者想重新配对：在家里电脑的「添加手机」里点那台设备旁边的「移除」。

---

## 8. 推送实测：几分钟后推一条

**做什么**：用每晚饮食汇总来测，因为它会按时间触发，不需要你去点手机。

1. 在家里电脑或手机的对话里对 Noyi 说：「每晚饮食汇总改到 HH:MM」，HH:MM 填当前时间往后 5 分钟（24 小时制，比如 `21:05`）。它会回复已开启和具体时间。
2. 【你手动】**关掉家里电脑上所有打开着 Noyi 的浏览器标签页**，iPhone 上把 Noyi 退到后台并锁屏。只要任何一个打开着的页面在最近 45 秒内报告过「我在看」，服务就认为你正在看，消息直接显示在页面上，不推送。
3. 等到设定时间之后 1 到 2 分钟。

**确认成功**：iPhone 锁屏上出现一条通知，内容是固定的一句「今晚的饮食汇总好了，点开查看」。点开后进入 Noyi，汇总消息就是对应那条。这时它才算「已送达」。

**失败了怎么办**：

- 一直没收到：从主屏幕打开 Noyi，页面顶部有「有 1 条提醒推送到手机后还没看到」，说明服务已经发出、苹果没送达或送晚了。这是苹果推送的已知行为，长时间没打开的主屏幕应用更容易被延迟。
- 页面顶部也没有这条提示，而对话里直接出现了汇总：说明发送时有页面开着，消息走了页面显示，没有推送。关掉所有页面再测一次。
- 一条都没有：当天已经发过汇总的话，同一天不会再发。改到明天再测，或者看 `server.log` 里有没有报错。
- 测完想恢复原来的汇总时间，在对话里再说一次「每晚饮食汇总改到 21:30」。

---

## 9. 升级和发版

### 9.1 家里现在装的是什么

安装时会直接装最新的家里版本。写这份清单时最新的是 `home-20261117-1`，指向提交 `adc3eda`。

```powershell
Get-Content D:\Noyi\current.json -Encoding UTF8
```

**确认成功**：`"tag": "home-20261117-1"`。

### 9.2 公司电脑发下一个版本

**做什么**：在公司电脑上，进入仓库根目录，确认 `main` 已推送、全量测试通过，然后打标签并推送。标签格式是 `home-YYYYMMDD-N`，同一天第二个版本序号加 1。标签必须指向 `origin/main` 上已有的提交，否则家里会跳过它。下面的命令把标签打在 `origin/main` 的最新提交上。

```powershell
git fetch origin; git status -sb
```

```powershell
$tag = 'home-' + (Get-Date -Format yyyyMMdd) + '-1'; git tag $tag origin/main; git push origin $tag
```

**确认成功**：`git ls-remote --tags origin 'home-*'` 里出现新标签。

**失败了怎么办**：提示标签已存在，就把末尾的 `-1` 改成 `-2` 再试。标签推错了不要删了重推同名标签，家里可能已经取到旧的那个；直接发一个序号更大的新标签。

### 9.3 家里确认已经升级

**做什么**：升级任务每 10 分钟检查一次，一次完整升级约 10 到 20 分钟。想马上检查，在普通 PowerShell 执行：

```powershell
python D:\Noyi\bin\upgrade.py --root D:\Noyi
```

**确认成功**：命令最后打印 `ok`；或者看 `upgrade.json`：

```powershell
Get-Content D:\Noyi\upgrade.json -Encoding UTF8
```

`last_result` 是 `ok`，`tag` 是新标签，`previous_tag` 是旧标签，`last_upgrade` 是刚才的时间。`current.json` 的 `tag` 也变成新标签。不在家时，也可以在 GitHub 上看 noyi-ops 的 `latest.json`，其中 `service.tag` 和 `service.upgrade_result` 是同一组信息，每小时更新一次。

**失败了怎么办**：

- 打印 `none`：没有比当前更新、且没失败过、且在 `main` 上的标签。看 `upgrade.json` 的 `detail`；以 `fetch:` 开头说明连不上 GitHub。
- 打印 `another upgrade is running`：计划任务正在升级，等它结束再看 `upgrade.json`。
- 打印 `rehearsal_failed`：新版本没通过准备或彩排，旧版本照常在跑，什么都没切换。`detail` 写着失败在哪一步（`checkout`、`install`、`smoke`、`backup`、`rehearsal`）。
- 打印 `health_failed_rolled_back`：新版本切换后 2 分钟内不健康，已自动恢复账本并退回旧版本。
- 打印 `rolled_back`：新旧版本都没恢复健康，**这是唯一需要你马上处理的情况**，按第 10.1 节手动回退。

失败过的标签记在 `upgrade.json` 的 `failed` 里，以后不再重试。修好后在公司电脑发一个新标签即可。
---

## 10. 暂停服务、手动回退上一版

### 10.1 暂停服务（hold 文件）

**做什么**：比如要手动动账本。`hold` 文件只是让守护进程**不再启动**服务，已经在跑的服务不会因此停下，所以要按下面三步来。第一步先停掉升级任务，因为升级过程会自己删掉 `hold` 文件。

```powershell
schtasks /Change /TN "\Noyi\Upgrade" /DISABLE
```

```powershell
New-Item -ItemType File -Force D:\Noyi\hold | Out-Null
```

然后执行文档开头的「重启服务」命令，这次它只会停下、不会再起来。

**确认成功**：

```powershell
Get-Content D:\Noyi\supervisor.json -Encoding UTF8
```

`state` 是 `held`，`pid` 是 `null`；<http://127.0.0.1:8765> 打不开。

**恢复**：

```powershell
Remove-Item D:\Noyi\hold
```

```powershell
schtasks /Change /TN "\Noyi\Upgrade" /ENABLE
```

几秒后 `state` 变回 `running`，页面能打开。

### 10.2 手动回退上一版

**做什么**：只有自动回退也失败（`upgrade.json` 的 `last_result` 是 `rolled_back`），或者新版本用着有问题、想退回时才需要。旧版本拒绝打开新版本改过的账本，所以必须同时恢复账本，不能只换代码。**从新版本切换后到现在写入的内容会丢失。**

1. 按 10.1 暂停服务（停升级任务、建 `hold`、停服务），确认 `state` 是 `held`。

2. 看当前版本和上一版本，确认上一版本的目录还在（只保留当前和上一版两个目录）：

   ```powershell
   Get-Content D:\Noyi\current.json -Encoding UTF8
   ```

   ```powershell
   Get-ChildItem D:\Noyi\releases
   ```

3. 找到切换到**当前版本**之前那一刻的账本备份，文件名是 `switch-<当前版本标签>-<时间>.sqlite`，取最新一份：

   ```powershell
   Get-ChildItem D:\Noyi\data\backups\switch-*.sqlite | Sort-Object LastWriteTime | Select-Object -Last 5 Name, LastWriteTime
   ```

4. 先把现在的账本另存一份，再用备份覆盖。下面的 `<当前版本标签>` 和备份文件名换成第 2、3 步看到的：

   ```powershell
   New-Item -ItemType Directory -Force D:\Noyi\data\backups\manual | Out-Null; Copy-Item D:\Noyi\data\state.sqlite* D:\Noyi\data\backups\manual\
   ```

   ```powershell
   Remove-Item D:\Noyi\data\state.sqlite-wal, D:\Noyi\data\state.sqlite-shm -ErrorAction SilentlyContinue; Copy-Item -Force D:\Noyi\data\backups\switch-<当前版本标签>-<时间>.sqlite D:\Noyi\data\state.sqlite
   ```

5. 把 `current.json` 指回上一版本。`<上一版本标签>` 填第 2 步看到的 `previous`。必须用这条命令写，用记事本或 `Set-Content` 写出的文件可能带 BOM，守护进程读到会当成「没有版本」：

   ```powershell
   [IO.File]::WriteAllText('D:\Noyi\current.json', (@{ tag = '<上一版本标签>'; previous = '<当前版本标签>' } | ConvertTo-Json -Compress))
   ```

6. 删掉 `hold`，服务以上一版本启动：

   ```powershell
   Remove-Item D:\Noyi\hold
   ```

7. **先不要重新打开升级任务**。升级任务只记得自动失败过的版本，你手动退回的那个版本没记在里面，打开后 10 分钟内它会再升级回去。等公司电脑发了修好的新版本，再执行：

   ```powershell
   schtasks /Change /TN "\Noyi\Upgrade" /ENABLE
   ```

**确认成功**：`supervisor.json` 里 `tag` 是上一版本、`state` 是 `running`；页面能打开，最近的对话和记录到切换那一刻为止都在。

**失败了怎么办**：服务起不来，`server.log` 提示账本版本比程序新：第 4 步用错了备份（比如用了 `pre-` 开头的或者别的版本的），按第 4 步换一份 `switch-<当前版本标签>-*` 重来。想撤销这次回退：再暂停一次，把 `backups\manual` 里的文件复制回 `D:\Noyi\data\`，把 `current.json` 改回原来的标签，再删 `hold`。
---

## 11. 常见卡点和排查

### 东西在哪

| 想看什么 | 位置 |
| --- | --- |
| 服务输出和报错 | `D:\Noyi\data\logs\server.log`，每次启动前有一行 `--- supervisor starting <标签> at <时间> ---` |
| 守护进程状态 | `D:\Noyi\supervisor.json` |
| 当前版本 | `D:\Noyi\current.json` |
| 最近一次升级结果 | `D:\Noyi\upgrade.json` |
| 最近一次日志回传 | `D:\Noyi\ops-last.json` |
| 账本备份 | `D:\Noyi\data\backups`，`pre-*` 和 `switch-*` 各保留最近 10 份 |
| 计划任务运行记录 | 事件查看器 → Microsoft → Windows → TaskScheduler → Operational |

```powershell
Get-Content D:\Noyi\data\logs\server.log -Tail 60 -Encoding UTF8
```

### supervisor.json 的 state

| state | 含义 | 怎么办 |
| --- | --- | --- |
| `running` | 正常 | 无 |
| `backoff` | 刚崩过，等 5 秒、30 秒或 2 分钟后重启 | 看 `server.log` |
| `crash_loop` | 同一版本 10 分钟内崩了 5 次，停止重启，等下一个版本 | 看 `server.log` 找原因；修好后发新版本，或按 10.2 回退 |
| `held` | 有 `hold` 文件 | 不是你建的，就是升级正在进行；升级结束后会自动删掉 |
| `waiting_lock` | 另一个进程占着数据目录（比如你手动起过一份） | 任务管理器里结束多余的 python 进程 |
| `no_release` | `current.json` 没有版本或读不出来 | 检查 `current.json`；是你手动写坏的，按 10.2 第 5 步重写 |
| `exited` | 守护进程按 `stop-supervisor` 文件正常退出了 | `schtasks /Run /TN "\Noyi\Service"` |

### upgrade.json 怎么看

- `checked`、`result`、`detail`：**最近一次检查**的时间和结果。每 10 分钟刷新一次，没有新版本时 `result` 是 `none`。
- `last_upgrade`、`last_result`、`tag`、`previous_tag`：**最近一次真正尝试升级**的时间、结果、目标版本和原版本。没有新版本时这几项保持不变。
- `last_result` 的取值：`install`（首次安装成功）、`ok`（升级成功）、`rehearsal_failed`、`health_failed_rolled_back`、`rolled_back`，含义见 9.3。
- `failed`：失败过的标签和失败的阶段，这些标签以后不再重试。

### 具体卡点

- **安装第 5 步很久没动静**：正常，它在下载 Python 和 Node 依赖、跑冒烟测试，网络慢时可能超过 20 分钟。单步超过 30 分钟会算作失败。
- **某个版本失败后不再重试**（包括首次安装失败）：先按 `detail` 修好原因（多半是网络或磁盘），然后在管理员窗口把它从失败名单里去掉，再手动升级一次：

  ```powershell
  $u = Get-Content D:\Noyi\upgrade.json -Raw -Encoding UTF8 | ConvertFrom-Json; $u.failed.PSObject.Properties.Remove('home-20261117-1'); [IO.File]::WriteAllText('D:\Noyi\upgrade.json', ($u | ConvertTo-Json -Depth 5))
  ```

  ```powershell
  python D:\Noyi\bin\upgrade.py --root D:\Noyi
  ```

  标签名换成 `failed` 里那个。
- **3 天后服务自己停了，或笔记本拔了电源就停**：用第 4 节的命令看 `TimeLimit`，不是 `PT0S` 就执行那里的修正命令。
- **重启后手机连不上，登录桌面后又好了**：Tailscale 没开无人值守模式，见 6.1。
- **日志回传失败**：`ops-last.json` 的 `ok` 是 `false`。`stage` 是 `push` 时，通常是 22 端口被挡或部署公钥没有写权限；没推出去的提交会留在本地，下个小时自动重推。`stage` 是 `export` 时，导出器拒绝了这份数据（校验没通过），什么都没发出去，看 `server.log`。
- **端口 8765 被占用**：公司那套桌面启动方式（`noyi start`）如果也装在这台电脑上，会抢同一个端口。家里电脑只用计划任务这一套。
- **改了 home.json 没生效**：设置只在服务启动时读取，执行一次开头的「重启服务」命令。
