# Personal Agent 接手开发者决策简报

## 1. M0、M1、M2 当前分别先做什么？D-A 为何覆盖旧排序？

**M0**：完成一个 Bot、真实模型 SDK、少量实际工具、持久 Task、真实产物与根要求验收 [legacy_handoff]。

**M1**：打通单层 Worker 完整闭环——同一 Bot 交错处理两项任务并正确接住后续输入，`spawn` 一律返回任务引用且后台执行 [decisions][legacy_handoff]。

**M2**：原 M1 的 Nudge 与进展判断顺延至此，同时承担 Worker 层职责：检查旧执行代次、子任务结果与交接恢复，根任务负责人持续验收 [decisions][legacy_handoff]。

**D-A 覆盖原因**：用户要求优先打通单层 Worker 闭环；后台续接是 Nudge 的前置条件，结束门禁需先区分"等子任务"与"真结束"，因此将原 M1 的 Nudge/进展判断推迟至 M2 [decisions]。

## 2. Pi 核心循环职责与本项目职责划分

**Pi 核心循环负责**：状态化 agent 与工具执行、AgentMessage 到 LLM Message 的转换、事件流发射（turn/message/tool 生命周期）、工具执行模式（并行/顺序）、steering 与 follow-up 队列管理、`prepareRequest` 与 `finishTurn` 钩子 [pi_core]。

**本项目还须负责**：Python 任务账本、千问官网 qwen3.7-plus 模型适配、受控工具实现、常驻 `pa serve` 进程及 Node 执行子进程管理 [decisions]。数据持久化沿用 SQLite [legacy_handoff]。接口与升级边界见 Pi 核心接入规范 [decisions]。

## 3. 上游验证日志读取情况

本次尝试读取 validation_log 失败，该文件缺失仅表示本次未能核对上游日志，与本产品测试是否存在或通过无关。UNAVAILABLE [validation_log]

其余资料已完整读取并以来源 ID 引用 [decisions][legacy_handoff][pi_core]。