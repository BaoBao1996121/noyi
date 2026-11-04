# Personal Agent 项目 M0 核对单

## 一、M0 真实交付验收边界

根据当前决策 [decisions]，M0 的收口工作已在 D-K 中明确映射："将研究建议映射到 M0 收口、M1-A/B/C"。M0 的核心是完成一个 Bot、一个真实模型 SDK、少量实际工具、持久 Task、真实产物和根要求验收 [legacy_handoff]。M0 不包含 follow-up 责任归属、权限分层或人工控制优先等增量机制，这些属于 M1 切片 A-D。

**验收边界结论**：M0 只需打通单层执行闭环，不要求实现 ThreadSubscription、AdmissionDecision 或 Attention 等研究交接中提出的复杂机制。若 M0 尚未闭环，应优先完成基础交付，而非推迟至研究方案全部落地 [legacy_handoff]。

## 二、M1 Worker / M2 Nudge 的当前顺序

决策 D-A [decisions] 已明确调整顺序：**单层 Worker 闭环提到 M1，原 M1（Nudge、进展判断）顺延到 M2**。原因是用户要求优先打通单层 Worker 的完整闭环，后台续接是 Nudge 的前置条件。

**重要澄清**：研究交接文档 [legacy_handoff] 第 6 节描述的"M1：同一 Bot 交错处理两项任务"及后续 M2 Worker 顺序，是上游研究的原始建议，**不能覆盖 D-A 的新决定**。D-A 至 D-K 均已获用户确认 [decisions]，具有更高优先级。当前实施应以 D-A 为准：M1 交付单层 Worker 闭环，M2 处理 Nudge 与进展判断。

## 三、pi-agent-core 接管的模型工具循环

决策 D-L [decisions] 明确：采用 pi-agent-core 1.5.0 替换自写模型/工具循环，状态为"**接入验证中**"。这意味着：

1. **模型工具循环**由 pi-agent-core 接管，其核心能力包括：AgentMessage 流转、convertToLlm 转换、tool_execution 事件流、prepareRequest/finishTurn 钩子、steering/follow-up 队列管理等 [pi_core]。
2. **本项目保留的责任**：
   - **SQLite 任务持久化**：pi-agent-core 不提供任务账本，Python 任务账本仍由本项目维护 [decisions]。
   - **恢复机制**：execution_epoch、requirement_revision、Artifact base_version 等持久事实由本项目实现 [legacy_handoff]。
   - **权限控制**：AdmissionPolicy、Authority 引用、Attention 等权限分层逻辑由本项目在工具层执行 [legacy_handoff]。
   - **验收责任**：根任务负责人持续负责验收，不以子 Agent 自报成功替代 [legacy_handoff]；pi-agent-core 仅提供执行框架，不承担业务验收。

**接口边界**：pi-agent-core 的 finishTurn 钩子可用于实现"结束后收到消息"的三种分别处理逻辑 [pi_core]，但具体归属判断、handback 记录、版本核对等业务逻辑仍由本项目实现。

## 四、模型确认

决策记录明确："模型遵循用户已指定的 Qwen 官方云端方向" [decisions]。结合任务要求，当前模型为 **qwen3.7-plus**。DeepSeek 研究的执行器选择不影响此决定 [decisions]。

## 五、验证日志状态

尝试读取 validation_log 源失败，返回 SourceUnavailable 错误。因此：

- **UNAVAILABLE [validation_log]**

必须强调：[legacy_handoff] 中提到的上游验证结果（"1,196/1,197 个测试通过，58/59 个测试文件通过"）是**上游研究项目的测试数据**，指向上游 VALIDATION.md 文件。**不得将此转述的上游测试通过数当作本项目已核实的测试证据**，更不能视为本产品测试状态。该日志仅证明上游原型参考项目的运行情况，与本项目当前实现无关。

若资料未提供本项目当前测试状态，只能标注为**未知**，不得推断为"不存在"或"未运行"。

## 六、D-L 状态澄清

决策 D-L 的状态明确为"**接入验证中**" [decisions]。**不得声称 D-A 至 D-L 全部已定**。D-A 至 D-K 为"已定（用户确认）"，但 D-L 仍处于验证阶段，采用库并不自动证明稳定 [decisions]。

## 七、证据边界总结

| 类别 | 来源 | 性质 |
|------|------|------|
| 当前决策 | [decisions] | 已确认的设计取舍，D-L 为接入验证中 |
| 研究交接 | [legacy_handoff] | 上游架构研究，提供增量建议但非当前产品状态；其中验收规格标为 specified_not_run |
| Pi 核心文档 | [pi_core] | 第三方库官方说明，描述接口能力而非本项目实现 |
| 验证日志 | UNAVAILABLE [validation_log] | 无法读取，不得引用其内容作为本产品测试证据 |

**严格区分**：
- **文档设计**：decisions 中的 D-A 至 D-K 为已定决策，D-L 为待验证选择。
- **来源转述**：legacy_handoff 中的上游测试数据、M1/M2 原始顺序仅为研究参考，不能覆盖 D-A 新决定。
- **已实测事实**：本项目当前无可用测试状态记录，所有关于测试通过的断言均为未知。

**禁止行为**：
1. 不得将 legacy_handoff 中的上游 VALIDATION.md 测试数据当作本项目测试证据。
2. 不得因 validation_log 不可读而推断本项目测试状态为"不存在"或"未运行"，只能标注未知。
3. 不得声称 D-L 已定，其状态为接入验证中。
4. 不得忽略 D-A 对 M1/M2 顺序的调整，仍以 legacy_handoff 原始顺序为准。

---

*本核对单仅生成文档，不执行其中任何命令。所有引用均基于实际读取的源内容，未读取源不得用于事实断言。*