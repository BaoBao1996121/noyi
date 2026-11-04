# Personal Agent Core

一个小型、可恢复的个人 Agent 执行内核。当前 M0 支持一个 Bot：接收明确任务，调用 Qwen 官方模型，读取指定资料，生成本地产物，检查产物并写入本地收件箱。SQLite 保存任务、输入、模型请求、工具记录和投递状态。

当前进度和真实验证结果见 [STATUS](STATUS.md)，设计与后续阶段见 [M0–M1 方案](docs/plan-m0-m1.md) 和 [DeepSeek 融合方案](docs/deepseek-integration-review.md)。本页的例子是可执行的虚构活动资料，不是已经完成的真实工作报告。

## 安装

需要 Python 3.11 或以上、Node.js 22.19 或以上。核心循环使用固定版本的 Pi Agent，接口边界见 [Pi 核心接入](docs/pi-kernel.md)。以下命令在仓库根目录的 Windows PowerShell 中执行；无需激活虚拟环境。

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e '.[test]'
npm ci --prefix .\src\personal_agent\pi_bridge --ignore-scripts
```

## 配置 Qwen 官方模型

默认模型是 `qwen3.7-plus`，默认地址是 Qwen 官网开放平台的 `https://maas.qianwenaiapi.com/compatible-mode/v1`。具体配置、其他官方账号地址和选型依据见 [模型配置](docs/model-configuration.md)。配置不会自动切换到其他提供方。

执行服务的 PowerShell 进程需要 `DASHSCOPE_API_KEY`。其他电脑若已设置该环境变量，可以直接使用；否则在当前窗口安全输入，不将密钥写进命令历史：

```powershell
$env:DASHSCOPE_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Qwen API Key' -AsSecureString)).Password
& .\.venv\Scripts\python.exe -m personal_agent model-config --config .\pa.example.toml
```

`model-config` 只显示非敏感配置，不调用模型，也不创建任务数据库。`.env.example` 是变量说明，程序不会自动加载 `.env`。`PA_MODEL` 和 `PA_BASE_URL` 可以覆盖配置文件，应与本机账号一致；不要把密钥提交到仓库。

## 跑一个完整任务

示例要求对比活动初稿与确认变更，生成最新执行清单、作废项及未确认事项。两份资料都是本地 UTF-8 文本，模型需要实际读取后交付，不能凭任务描述直接宣称完成。

先提交任务。提交只保存请求，尚不调用模型：

```powershell
$submitted = & .\.venv\Scripts\python.exe -m personal_agent --data .\.pa\m0-demo submit --spec .\examples\m0-task.json --nonce demo-workshop-v1
if ($LASTEXITCODE -ne 0) { throw '任务提交失败' }
$taskId = ($submitted | ConvertFrom-Json).task_id
$taskId
```

相同 nonce 与相同内容重试提交，会返回同一任务；相同 nonce 携带不同内容会被拒绝。若修改了示例要求，使用新 nonce 创建另一项任务。JSON 中本地资料路径相对于任务 JSON 文件所在目录解析。

随后在已配置密钥的窗口执行服务。下面会处理当前可执行任务，完成后退出：

```powershell
& .\.venv\Scripts\python.exe -m personal_agent --data .\.pa\m0-demo serve --config .\pa.example.toml --once
```

如需持续接收任务，去掉 `--once`，让这个服务窗口保持运行。服务管理 Pi 的 Node 执行子进程。在另一个窗口可以继续 submit、status 或 inbox；关掉客户端不影响服务，退出服务才会停止执行。中断的任务在下次启动服务时恢复，已使用预算不会被重置。

查看任务和本地投递：

```powershell
& .\.venv\Scripts\python.exe -m personal_agent --data .\.pa\m0-demo status $taskId --detail
& .\.venv\Scripts\python.exe -m personal_agent --data .\.pa\m0-demo inbox
```

在新的 PowerShell 窗口里没有 `$taskId` 时，先运行不带 ID 的 `status`，再把返回的 ID 传给详细查询。全局选项 `--data` 放在子命令前。

任务详情包含模型请求、输入使用记录、工具回执与验收结果。产物在 `.pa/m0-demo/tasks/<task_id>/artifacts/`，投递在 `.pa/m0-demo/inbox/`。投递 accepted 表示本地收件箱已接受，不代表人已经阅读。

示例初始预算为最多 12 次模型调用、40 次工具调用和 600 秒任务时间。真实调用会使用账号额度；费用是否可知应以实际返回的用量和账号计费记录为准。

## 失败、暂停与明确重试

先检查 `status <task_id> --detail` 中的失败类型、预算与已保存结果。`resume`（也可用 `retry`）只受理 paused、failed 或 review 状态，重新排入队列，不自行启动服务：

```powershell
& .\.venv\Scripts\python.exe -m personal_agent --data .\.pa\m0-demo resume $taskId
```

预算已经用尽时，必须明确增加对应预算；已使用次数仍保留。按实际需要增加，不必每次都加三个维度：

```powershell
& .\.venv\Scripts\python.exe -m personal_agent --data .\.pa\m0-demo retry $taskId --extra-calls 3 --extra-tools 8 --extra-seconds 120
& .\.venv\Scripts\python.exe -m personal_agent --data .\.pa\m0-demo serve --config .\pa.example.toml --once
```

总预算仍受 TaskSpec 上限约束：100 次模型调用、200 次工具调用、3600 秒。SDK 不隐藏重试；已完成任务不会因 retry 再执行。新增工作请另行提交任务。

## 本地测试

协议测试使用可控模型和本地故障注入，不需要真实密钥，也不调用云模型：

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
npm test --prefix .\src\personal_agent\pi_bridge
```

也可运行仓库脚本，让测试临时文件保留在忽略的 `.pa/test-temp/` 内，并设置 UTF-8：

```powershell
& .\scripts\run-tests.ps1
```

脚本不会删除已有任务数据。Windows 测试需要能创建临时目录和初始化 asyncio 的普通本地运行环境。协议测试通过与真实模型/真实渠道验证分开报告；本地模拟结果不能冒充云端闭环。

## M0 的边界

- 一个 Bot、单个活动 Run，工具限于指定资料读取和任务目录中的草稿产物；没有 shell 工具、Worker、外部消息发送或系统定时器。
- `chat` 是无工具的普通聊天入口；执行工作使用 `submit`。后续输入归属、运行中补充、细粒度停止/取消、持久问题及 Worker 闭环属于后续 M1。
- `submit_result` 检查产物完整性、资料读取覆盖、引用和用户指定文本条件。`succeeded/acceptance=passed` 表示这些明确检查通过，不证明所有语义、推理或事实都正确。真实任务仍需按根要求检查内容；文件存在或关键词出现不能取代结果质量验收。
- 本地文件投递可按 delivery ID 重放；没有承诺第三方平台 exactly-once。未接入的渠道没有端到端验证。
- 服务在当前电脑当前用户权限下运行，资料与产物保留在本地数据目录。只授权任务所需资料；读取到的文件内容是证据，不会因此获得执行权限。

`.pa/`、本地配置、虚拟环境和数据库默认不进 Git。架构资料来自研究，不代表上游代码或上游测试成绩已经成为本产品能力。
