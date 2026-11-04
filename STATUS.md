# 项目交接状态

更新：2026-11-04。

**M0、M1 与提前版 WebUI 已完成本轮验收；完整目标为 M1–M5 全部完成。** 核心循环使用 Pi Agent，模型使用千问官网 `qwen3.7-plus`。M1 已具备后台 Worker、补充责任、停止/取消、持久问题与恢复。UI 已跑通浏览器真实任务及提问/回答；M2–M5 尚未完成。

## 接手入口

1. [README](README.md)：安装、模型配置、提交、运行、查状态与重试。
2. [M0 验收报告](docs/validation/M0_REPORT.md)：逐项证据、真实任务结果、失败和限制。
3. [Pi 核心接入](docs/pi-kernel.md)：固定版本 1.5.0、受管 Node 子进程、Python 任务层边界。
4. [模型配置](docs/model-configuration.md)：Qwen 官网地址、准确型号、环境变量；不含凭据。
5. [决策](docs/decisions.md)、[M0–M1 计划](docs/plan-m0-m1.md)、[融合方案](docs/deepseek-integration-review.md)。

最新：[M1 验收报告](docs/validation/M1_REPORT.md)，149 项 Python 与 11 项 Node 测试；真实 Qwen 双 Worker、暂停/补充/重启/复核/整合闭环，全队 21 次调用，重复启动新增调用 0。后续以 [完整计划](docs/plan-m1-m5.md) 为准。

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

## 下一阶段

推进 M2 结束门禁、进展、审核返工及产物版本，随后 M3、M4、M5。[WebUI 验收](docs/validation/UI_EARLY_REPORT.md)新增6项 HTTP 测试和两条浏览器真实 Qwen 闭环。B01–B15、DS04–DS11 已覆盖，后续阶段场景未宣称通过。一般语义质量仍需结合来源独立复核。
