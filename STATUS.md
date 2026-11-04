# 项目交接状态

更新：2026-11-05。

**M1–M5 全部完成，本轮交付为 0.5.0 本地自用版。** 核心循环使用 Pi Agent，模型使用千问官网 `qwen3.7-plus`。已具备后台协作、补充/控制、持久问题、审核返工、多 Bot、群路由、显式记忆、持续目标、真实周期/资料变化检查、有界新工作准入、证据复盘与观察计划。完整 WebUI、后台启动/停止、备份恢复和独立安装已验证。

## 接手入口

1. [README](README.md)：安装、模型配置、提交、运行、查状态与重试。
2. [M0 验收报告](docs/validation/M0_REPORT.md)：逐项证据、真实任务结果、失败和限制。
3. [Pi 核心接入](docs/pi-kernel.md)：固定版本 1.5.0、受管 Node 子进程、Python 任务层边界。
4. [模型配置](docs/model-configuration.md)：Qwen 官网地址、准确型号、环境变量；不含凭据。
5. [决策](docs/decisions.md)、[M0–M1 计划](docs/plan-m0-m1.md)、[融合方案](docs/deepseek-integration-review.md)。

自用入口：[操作指南](docs/self-use-guide.md)，双击 `Start Personal Agent.cmd`。最新验收：[M5 报告](docs/validation/M5_REPORT.md)，**221 项 Python、12 项 Node 通过**；M5 共 12 组真实对照、2 组提示调优、5 项浏览器发起的复盘任务，累计 294 次 Qwen 调用。失败、恢复、措辞问题和未测边界均保留，不将结构检查通过等同于内容正确或长期有效。

各阶段：[M1](docs/validation/M1_REPORT.md)、[提前 UI](docs/validation/UI_EARLY_REPORT.md)、[M2](docs/validation/M2_REPORT.md)、[M3](docs/validation/M3_REPORT.md)、[M4](docs/validation/M4_REPORT.md)、[M5](docs/validation/M5_REPORT.md)。范围以 [完整计划](docs/plan-m1-m5.md) 为准。

## M0 历史证据

| 项目 | 结果 |
|---|---|
| Python 离线测试 | 119 项通过；Runtime 测试启动真实 Node/Pi，用可控模型模拟时序与故障 |
| Node 桥接测试 | 11 项通过；实际 Pi Agent 循环、参数拒绝、截断、消息保真和关闭协议 |
| 真实官网调用 | 官方 endpoint + qwen3.7-plus；最后一次任务 9 次模型请求、9 次工具调用、约 44.3 秒 |
| 真实资料与产物 | 完整读取三份本地文档和一份固定版本 HTTP 原文；一个可选日志真实读取失败并明确披露；产物及哈希留存 |
| 重启与重复提交 | 对最后一次真实任务重新打开数据库、重复提交、启动服务，任务/请求/工具/投递计数不变，新增模型调用为 0 |
| 安装入口 | README 的 editable 安装通过；pa model-config、示例 submit/status 已实际检查 |
| 内容质量 | 初步长文存在范围混淆，未被机械检查发现；全部保留。最终补齐当前详细计划后，决策简报的具体结论经过来源对照复核 |

用户已确认 DeepSeek 融合方案并恢复开发，随后提出复用 Pi 核心循环。D-A 至 D-K 保留，D-L 记录 Pi 的具体接入与进程变化。上游研究的原 ZIP、原 30 个场景、来源索引和原始日志仍未补齐；它们不冒充本产品验收。

## 交付与后续自用

本轮 M1–M5 没有待完成阶段。后续以用户自己的资料和持续目标收集真实反馈，观察多日稳定性、纠错负担和实际收益。两组短资料场景中单体平均调用更少，因此短而连贯的任务优先直接执行；不能外推为所有任务都不应协作。Nudge 在 M5 对照中自然触发 0 次，未证明收益。

复盘的事实、用量和观察范围由冻结账本生成，模型解释与原始草稿单独保留。原始模型文本仍出现过责任范围扩大和计数误读，重要结论需要对照来源。没有测量的人工耗时、业务效果和账单金额保持未知。

本机日常数据使用 `.pa/personal`；验收数据另存，不把演示任务和观察计划混进自用库。备份不含专用模型密钥，恢复写入新目录并暂停未完成工作。接口见 [模块说明](docs/module-interfaces.md)；本版未承诺 DSH/Pi 双生态原样兼容，也未接入远程调度、邮件、日历或外部消息投递。
