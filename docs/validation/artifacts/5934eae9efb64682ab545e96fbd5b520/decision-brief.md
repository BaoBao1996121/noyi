# Personal Agent 接手决策简报

## 1. M0、M1、M2 当前分别先做什么？

依据 [current_plan] 与决策 D-A [decisions]，任务切片顺序已调整：

- **M0**：完成单 Bot 真实交付闭环。包括 SQLite（WAL）存储、Qwen 适配、常驻 `pa serve` 进程、CLI 客户端、真实模型 + 只读工具 + 产物验收。调度器、`messages`、`events` 从第一天就建好，收口请求输入证据与恢复规则 [current_plan]。

- **M1**：实现单层 Worker 闭环。包含 `spawn` 异步派生、总控等待子任务、结果事件入库、条件续接、验收交付、停止/取消/预算控制。分 A（输入归属与控制入口）、B（Worker）、C（持久问题与整体闭环）三个增量 [current_plan]。决策 D-A 明确将单层 Worker 闭环从原 M2 提前至 M1，因为用户要求优先打通 Worker 完整闭环，且后台续接是 Nudge 的前置 [decisions]。

- **M2**：承接原 M1 内容，包括结束门禁扩展、Nudge、进展/停滞判断、调查记录，以及原 M2 余下的 handoff、review 返工、产物 base_version [current_plan]。

**为何覆盖 legacy_handoff 的旧排序**：[legacy_handoff] 建议 Worker 在 M2、Nudge 在 M1，但该建议已被决策 D-A 正式覆盖。D-A 于 2026-11-04 确立"单层 Worker 闭环提到 M1，原 M1 顺延到 M2"，原因是用户明确要求优先打通 Worker 闭环，且结束门禁需先能区分"等子任务"和"真结束" [decisions]。旧版 M2 Worker 整段不能直接并入新版 M2，执行代次、子任务结果恢复等按 [current_plan] 的 M1 闭环及 B 组验收归属处理；M2 剩余范围也以 [current_plan] 为准。

## 2. Pi 核心循环负责什么，本项目还须负责什么？

模型使用千问官网 qwen3.7-plus [decisions]。

**Pi 核心循环（pi-agent-core）职责** [pi_core]：
- 管理 AgentMessage 与 LLM 消息转换
- 执行模型调用与流式响应
- 工具调用的预检（beforeToolCall）、执行、后处理（afterToolCall）
- Turn 生命周期管理（turn_start/turn_end）
- Steering 和 Follow-up 队列管理
- 事件流订阅（agent_start/end、message_update、tool_execution 等）
- 通过 finishTurn 决定是否继续下一轮

**本项目仍须负责** [decisions][current_plan]：
- Python 任务账本与 SQLite 持久化（唯一权威状态）
- Qwen 适配层（qwen3.7-plus 接入）
- 受控工具集（只读或草稿写入）
- 调度器：收集待处理输入、检查门禁、按 wait_spec 判断时机、事务性插入 Run、崩溃恢复
- events 表的认领/消费两阶段机制
- 停止与取消契约（paused/forbidden/on 状态转换）
- 输入归属（ThreadSubscription、FollowupDisposition）与责任留证（InputUsage）
- 常驻服务进程管理与 CLI 客户端

采用 pi-agent-core 并不自动证明稳定，仍以当前实现的测试与真实任务为准 [decisions]。

## 3. 上游验证日志读取情况

尝试读取 validation_log 失败：UNAVAILABLE [validation_log]。该文件缺失只表示本次未能核对上游日志，与本产品测试是否存在或通过无关。

其余资料已完整读取并引用来源 ID：[current_plan]、[decisions]、[legacy_handoff]、[pi_core]。