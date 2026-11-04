# M0 验收记录 · 2026-11-04

结论：单 Bot 的持久真实任务闭环已跑通；Pi 核心接入及必要协议/故障验收通过。一般内容正确性仍需要独立复核，不能把 `acceptance=passed` 理解为通用语义证明。

## 实现与架构

使用 `@earendil-works/pi-agent-core@1.5.0` 的真实 Agent 循环。Python 提供模型与工具回调，不再保留另一套自写模型/工具循环。一个 `pa serve` 管理 SQLite WAL 和受管 Node 子进程；Node 不接收 API key，只注册资料读取、产物写入和结果提交工具。

模型为 Qwen 官网 `https://maas.qianwenaiapi.com/compatible-mode/v1` 的 `qwen3.7-plus`，非思考模式、输出上限 4096、单请求超时 90 秒。SDK 无隐藏重试。任务、执行、输入使用、操作、验收和投递分别留存；失败投递不会重做成功业务。

## 原始 M0 要求逐项核对

| 要求 | 本轮证据 | 结果 |
|---|---|---|
| 完整读交接与融合研究 | 原交接已完整阅读；新增两份 DeepSeek 文件全文阅读及相关上游核对，缺件列明 | 已完成可取得资料；不声称读过缺失包 |
| Bot、Task、Run、证据持久化 | actors/rooms/tasks/runs/messages/events/steps/operations/model_requests/input_usage/deliveries；SQLite WAL | 通过 |
| 真实 ModelAdapter | 官网返回 model、request_id、用量、耗时；凭据不入仓库 | 通过 |
| 本地与 HTTP 只读工具 | 最终任务读取三份本地资料和固定 commit 的 Pi README，长材料逐页到 EOF | 通过 |
| 旧版本/失效来源 | 旧 Worker 顺序与 current_plan 冲突；上游 VALIDATION.md 缺失，真实失败加 UNAVAILABLE 标记 | 通过 |
| 真产物、来源与要求验收 | 文件、哈希、读取覆盖、引用、required_text；具体结论另做来源对照 | 通过，范围见下 |
| 工具错误不报成功 | 未授权工具、错误参数、缺失资料、截断参数负例；截断工具不执行，修正最多一次 | 通过 |
| 重复输入不重复工作 | 双客户端/nonce 测试；真实任务重启重提后计数不变 | 通过 |
| 状态与交付可查 | CLI submit/status/model-config 实测；运行、请求、操作、收件箱回执可查 | 通过 |
| 已接受任务跨重启保留 | claim/request/产物/投递崩溃边界测试；真实已完成任务重新启动不重跑 | 通过 |
| 有界执行与权限 | 持久预算、明确扩展、当前 Run/版本检查、来源白名单、目录隔离、无任意 shell | 通过 M0 范围 |

## 测试

Python：**119 passed in 7.47s**。Node 桥接：**11 passed**。Python Runtime 测试实际使用安装的 Node/Pi，仅模型使用可控替身；协议测试不消耗云模型额度。

覆盖重复/冲突 nonce、受众检查、每 actor 一个活动 Run、旧代次拒绝、事务回滚、请求中断 unknown、预算不退款与长停机封顶、工具超时终态、缺产物不成功、部分读取不算全文、来源错误注入、不完整参数不执行、重复截断暂停、产物写出后回执前中断的原操作对账、投递确认丢失的幂等重放、Node 关闭和凭据隔离。

对应测试：[Store](../../tests/test_store.py)、[Runtime](../../tests/test_runtime.py)、[Pi 传输](../../tests/test_pi_transport.py)、[工具](../../tests/test_tools.py)、[工具契约](../../tests/test_tool_contracts.py)、[模型/服务](../../tests/test_model_service.py)、[Node 桥接](../../src/personal_agent/pi_bridge/bridge.test.mjs)。

复现命令（仓库根目录）：

```powershell
& .\scripts\run-tests.ps1
npm test --prefix .\src\personal_agent\pi_bridge
```

开发时受限 Windows 进程的 asyncio 初始化曾挂起；在获准的普通本地进程测试通过。这不证明产品具备 OS 隔离。

## 真实任务

最终任务 ID：`5934eae9efb64682ab545e96fbd5b520`。回答当前 M0/M1/M2 顺序、旧版顺序如何被覆盖、Pi 与宿主职责、上游验证日志是否读到。

- 9 次真实模型请求、9 次工具调用，约 44.3 秒；任务 succeeded，机械验收 passed，本地投递 accepted。
- 完整读取 current_plan、decisions、legacy_handoff 和固定 commit 的 pi_core HTTP 原文；可选 validation_log 的真实缺失已披露。
- [最终模型产物](artifacts/5934eae9efb64682ab545e96fbd5b520/decision-brief.md)，未人工改写，SHA-256：`9109d64607eb4e00adcb2a8efb0b7811e53ea3602f30d8c045f9322706ccc9aa`。
- 内容对照：M1 Worker、M2 Nudge 及余下 handoff/review/base_version 归属正确；Pi 与本项目职责分开；Qwen 型号正确；未把缺失上游日志推成产品未测试。约 620 汉字，稍长于期望的约 400–600 字，记录为非阻断呈现偏差。
- 真实重启重提后，4 个 Pi 任务、4 个 Run、33 次请求、33 个操作、4 个 Delivery 的计数完全不变；新增模型调用为 0，最后任务仍只有一份投递。

复现输入见 [live-task.json](live-task.json)，当前方案/决策使用当时快照，HTTP 使用固定 commit；缺失日志保持可选缺件。复现会产生真实 API 调用，凭据通过环境变量提供。

## 保留的失败与限制

共保留 5 项开发期真实任务：一项旧循环、四项 Pi 循环。旧循环出现三次输出上限导致的错误写文件参数，均未执行不完整写入；随后补上 finish_reason、Pi 原生截断拒绝及一次修正上限。

前三份 Pi 产物机械检查通过，内容复核仍发现：缺上游日志被误推为产品无测试、决定状态夸大、旧 M2 Worker 职责混入新顺序。证据中标为 needs_revision，不作为最终内容通过依据。最终补齐当前详细计划这一权威来源、明确问题边界后，获得上述简报。

Pi 负责循环可靠性，不保证模型推理正确。资料完整性和版本优先级影响结果；M0 机械门禁发现不了所有语义错误。后续需持续评测，不能宣称自主验收一切内容。

[m0-evidence.json](m0-evidence.json) 包含全部任务的用量、工具成功/失败、输入哈希、产物、内容复核和真实重启防重结果。累计提供方返回用量：输入 **441,584 tokens**、输出 **23,701 tokens**，包含弃用草稿与失败尝试。账单费用未查询，人民币金额未知；此处不是开发会话自身的模型用量。

M1 Worker、运行中补充归属、完整停止/取消 UI、Attention、M4 主动规则和外部消息渠道尚未完成。投递 accepted 只表示本地收件箱接收，human_read 为 unknown。原 30 项研究场景与原日志仍缺失。

代码/测试哈希见 [code-sha256.json](code-sha256.json)，按 UTF-8 文件字节将 CRLF 统一为 LF 后计算 SHA-256，避免各电脑的 Git 换行设置造成假差异。原始模型产物和输入快照保留原字节，其回执哈希不作换行转换。本报告不以研究上游测试数字替代本产品结果。
