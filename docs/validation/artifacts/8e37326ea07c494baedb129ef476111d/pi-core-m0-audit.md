# Personal Agent M0 核对单

## 一、M0/M1/M2 交付顺序与验收边界

### 当前有效顺序（以决策记录为准）

根据 [decisions] 中 D-A 决定（2026-11-04 已定）：**单层 Worker 闭环提到 M1，原 M1（Nudge、进展判断）顺延到 M2**。原因是用户要求优先打通单层 Worker 的完整闭环，后台续接是 Nudge 的前置条件。

因此当前有效顺序为：
- **M0**：基础闭环收口（一个 Bot、真实模型 SDK、少量实际工具、持久 Task、真实产物与根要求验收）[decisions][legacy_handoff]
- **M1**：单层 Worker 闭环（同一 Bot 交错处理两项任务，并接住正确的后续输入；`spawn` 一律返回任务引用、后台执行）[decisions]
- **M2**：Nudge 与进展判断（原 M1 内容顺延至此）[decisions]

### 研究交接的旧顺序不能覆盖新决定

[legacy_handoff] 第 6 节给出的参考切片是"M1：同一 Bot 交错处理两项任务"，其上下文基于交接时的研究假设，并未包含 D-A 对 Nudge 顺延的调整。该文档本身在第 10 节明确声明："本交接包完成的事：固定证据版本、比较既有设计、提供增量选择、给出验收规格"，"仍需开发会话完成的事：对照当前代码，实施必要增量，运行本产品的真实验收与持续使用验证"。**研究交接是架构研究的增量交接与待执行验收规格，不是当前产品实现状态**[legacy_handoff]。当 [decisions] 中出现经用户确认的 D-A 至 D-K 决定时，旧顺序已被覆盖，不得以研究转述作为当前交付依据。

### M0 真实交付验收边界

结合 [decisions] D-K（将研究建议映射到 M0 收口）与 [legacy_handoff] 第 6 节"如果 M0 尚未闭环"的要求，M0 的最小交付包括：
1. 一个 Bot 接入真实模型 SDK
2. 少量实际可用工具
3. 持久化 Task 机制
4. 真实产物生成与根任务负责人验收（不以子 Agent 自报成功替代）[legacy_handoff]

M0 不要求事件来源完整、多任务交错或 Nudge 能力，这些属于 M1/M2。

## 二、模型工具循环与 SQLite 任务持久化的分工

### pi-agent-core 接管的部分

根据 [decisions] D-L 决定，项目采用 **pi-agent-core 1.5.0** 替换自写的模型/工具循环。[pi_core] 文档说明该库负责：
- 模型请求与响应流式处理（`agentLoop` / `Agent.prompt`）
- 工具调用的预检、执行、结果收集（支持 parallel/sequential 模式）
- Turn 生命周期管理（`prepareRequest` → provider response → tool results → `finishTurn` → `turn_end`）
- Steering 与 Follow-up 队列调度
- 事件订阅机制（`agent_start` / `turn_start` / `message_update` / `tool_execution_end` / `agent_end` 等）[pi_core]

### 我们保留的部分

[decisions] D-L 明确：**保留 Python 任务账本、Qwen 适配和受控工具；一个 serve 管理 Node 执行子进程**。具体包括：
- **SQLite 任务持久化**：Task/Run/Artifact/Evidence 等对象的持久存储、事务提交与恢复 [legacy_handoff][decisions]
- **恢复机制**：execution_epoch 识别当前执行绑定，阻止旧 Worker 回调提交当前结果；requirement_revision 标识用户要求版本 [legacy_handoff]
- **权限分层**：阅读/接单/动作权限分层；M4 的 AdmissionPolicy 独立于周期 Policy；observe 只限制未启用的新推断规则，不覆盖已有明确授权 [decisions] D-J
- **验收责任**：根任务负责人持续负责，委派不转移最终验收责任 [legacy_handoff]

pi-agent-core 不管理任务账本、不处理跨进程的持久化恢复、不承担业务层面的权限判定与验收逻辑。它仅提供模型交互与工具执行的底层循环能力。

## 三、模型选型

根据 [decisions] "模型遵循用户已指定的 Qwen 官方云端方向"，当前模型为 **qwen3.7-plus**（千问官网）。该选择不受 DeepSeek 研究的执行器选择影响 [decisions]。具体型号细节与尚未验证项参见项目内的模型配置文档（本核对单不展开）。

## 四、验证日志状态

尝试读取附带验证日志 [validation_log]，但该源不可用。**UNAVAILABLE [validation_log]**

需要特别澄清：[legacy_handoff] 中提到上游原型的测试结果为"1,196/1,197 个测试通过，58/59 个测试文件通过"，但这是指 DeepSeek Super-agent 上游仓库的验证状态，**不是本 Personal Agent 产品的测试结果**。该文档明确声明："没有改动上游源码或 lockfile，没有执行真实模型、真实 GitHub/飞书账号端到端验证"，且 acceptance_cases.json 中所有场景均标为 `specified_not_run`[legacy_handoff]。**不得把研究转述的上游测试通过数当作本产品已核实的事实，更不能把它当作本产品测试**。

## 五、缺件清单

由于 [validation_log] 不可用，以下验证证据缺失：
- 本产品 M0 切片的真实协议测试运行日志
- 真实模型任务交错执行的验收记录
- 真实渠道断连恢复与投递计数证据

当前核对单仅基于三份已读文档的设计约定与决策记录，不包含任何实测通过数据。

---

**引用来源：**
- [decisions]：决策记录，含 D-A 至 D-L 全部已定决定
- [legacy_handoff]：DeepSeek 研究交接文档，性质为架构研究而非当前实现
- [pi_core]：pi-agent-core 官方文档，说明模型工具循环的事件流与 API
- UNAVAILABLE [validation_log]：验证日志源不可用