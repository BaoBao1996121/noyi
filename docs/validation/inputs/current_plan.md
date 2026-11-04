# M0–M1 实施方案：单 Bot 交付 + 单层后台 Worker 闭环

日期：2026-11-04。状态：已有未验收的本地 M0 草稿；融合设计已确认，M0 开发进行中。

输入：交接设计（`temp` 仓库 `02_UPDATED_DESIGN.md`、`03_IMPLEMENTATION_AND_EVAL.md`）与 Codex 的《后台任务与自动续接设计》《实施交接说明》。本文在两者之间取一套口径，作为 M0–M1 的实施依据；与交接设计不同的地方记在 [decisions.md](decisions.md)。

2026-11-04 增补：[DeepSeek 融合方案](deepseek-integration-review.md) 已并入本文的输入、恢复和切片设计。新增语义已获用户确认，不能当作已实现或已测试；既有 D-A 至 D-E 保持有效。当前事实以 [STATUS](../STATUS.md) 为准。

## 1. 现状核实

- 原方案编写时无产品代码；当前本地已有 `src/personal_agent/` 等未提交 M0 草稿，包含存储、Qwen 适配、运行器、少量工具、常驻服务和 CLI，未完成测试或真实闭环。
- 所以下文的「已具备」只表示交接设计里已有对应规则，不表示代码已实现。
- 草稿存在不等于执行器已可验收。恢复开发时按当前代码逐项验证第 6 节；本轮按确认后的方案收口实现并验证。

## 2. 概念对齐

不新增平行的任务平台，Codex 的五个概念映射到交接设计已有的概念上：

| Codex | 本方案 | 说明 |
|---|---|---|
| Session 会话 | Room | 私聊即一个 Room；一个 Room 可承载多项委托 |
| Assignment 委托 | 根 Task（`parent_task_id` 为空） | 带验收、需求版本、预算、续接开关、取消状态；M4 再加 Goal 作为根 Task 的上层 |
| Task 任务 | 子 Task | M1 只有一层：根 Task 下的 Worker 子任务 |
| Turn 总控轮次 | 负责人 Bot 的 Run | Run 统一表示任何 actor 的一次执行，用 `actor_id` 与 `trigger` 区分 |
| Run 执行 | Worker 的 Run | 重试、恢复都生成新 Run；`execution_epoch` 校验旧 Run 迟到结果 |

## 3. 对照结论

| Codex 机制 | 判定 | 处理 |
|---|---|---|
| 轮次结束 ≠ 委托结束；等待不占执行资源 | 已具备（设计） | 沿用：结束门禁返回 WAIT 并落盘唤醒条件 |
| 用 run 版本拒绝迟到结果 | 已具备（设计） | 沿用 `execution_epoch` / `requirement_revision` |
| 同伴/Worker 文本不授予权限 | 已具备（设计，T23） | 续接输入把 Worker 结果标为数据 |
| 入站去重、出站 outbox 同事务 | 已具备（设计，T02/T04） | 沿用 |
| 完成事件 + 消费游标 + 崩溃可恢复 | 需补齐 | 新增 `events` 表与认领/消费两阶段（4.2） |
| 总控同时只跑一轮，并发完成合并 | 需补齐 | 每 actor 一个活动 Run 的唯一约束 + 批量认领（4.3） |
| 停止回复 / 停止任务 / 取消委托三种语义 | 需补齐 | 交接设计只有取消事实，新增停止契约（5） |
| 自动续接开关（停止后暂停） | 需补齐 | 根 Task 加 `auto_continue`：on / paused / forbidden |
| 输入保存、请求使用与处理责任分开 | 需补齐（本次增量已确认） | 用 InputUsage 记录每次请求纳入的输入，disposition 记录责任结论；`adopted_by_run` 仅作兼容摘要，晚到补充仍保留 |
| 多次回报：阶段 / 最终 / 失败 / 已停止 | 需补齐 | 事件带 `kind` 与 `seq`，按 event_id 去重而非 task_id |
| 自动续接次数预算 | 需补齐 | 并入 D08 的预算维度，按根 Task 计 |
| 任务切片顺序 | 冲突 | 交接设计 Worker 在 M2、Nudge 在 M1；按本次要求把单层 Worker 闭环提到 M1，Nudge 顺延 M2（D-A） |
| 前台等待转后台（6.5） | 暂缓 | M1 的派生一律后台返回任务引用，不提供前台等待，6.5 的竞争场景不出现（D-B） |
| 退出应用后是否继续 | 本方案决定 | 常驻 `serve` 进程 + CLI 客户端（D-C） |
| 系统通知、“用户在场”判断 | 暂缓实际通知 | M1 只记录哪些投递值得提醒，在场 = 有 `chat` 客户端心跳 |
| Worker 启动后台命令的归属 | 不适用 | M1 工具集没有 shell，等加 shell 工具时再接入 |

## 4. 运行结构

### 4.1 进程与入口

- `pa serve`：常驻服务进程，单进程 asyncio，有界并发，SQLite（WAL）为唯一权威状态。
- `pa chat / submit / status / stop / cancel / continue`：CLI 客户端，只读写数据库并唤醒服务，关掉 CLI 不影响服务。
- 每个 Run 是服务里独立的 asyncio 任务。Worker 的 Run 从第一天就不挂在总控 Run 的取消作用域下，“总控结束本轮、Worker 继续”是默认形态，不需要“转后台”动作。
- 停掉 `serve` 就停掉全部执行，结果和进度保留。界面如实说明这一点，不暗示后台仍在跑。

### 4.2 记录（在交接设计 M0 表基础上增量）

| 表 | M0–M1 关键字段与增量 |
|---|---|
| `messages` | 来源/账号与 `client_nonce` 防重、`kind`（user / system_event / peer / delivery）、保存状态、输入版本；`adopted_by_run` / `applied_revision` 仅作摘要，不证明责任解决 |
| `input_usage`（提案） | message/event、run、请求序号、内容版本/哈希、请求状态；认领与实际模型请求分别留证 |
| `thread_subscriptions` / `followup_dispositions`（提案） | own/context、受众引用；follow-up 的唯一负责根 Task、责任代次、状态、处理依据与交回历史 |
| `attentions`（复用 wait/Approval 的提案） | question_id、task、答复者、requirement_revision、information/authorization/review、状态、resolution、投递引用 |
| `tasks` | `root_task_id`、`result_receiver`、`auto_continue`、`cancel_state`、`wait_spec`（等哪些子任务、any/all、截止时间）、续接计数 |
| `runs` | `trigger`、`phase`（running / waiting_children / finalizing / ended）、`end_reason`、`stop_requested_at`、`stopped_at`、心跳 |
| `events` | `event_id`、`root_task_id`、`task_id`、`run_id`、`seq`、`kind`（partial / final / failed / stopped / needs_user / interrupted）、`result_ref`、`receiver`、`state`（pending / claimed / consumed / archived）、`claimed_by_run`、`consumed_by_run` |

写入顺序：Worker 先写产物，再在一个事务里写结果引用、更新 Task、插入事件。事件不会指向没写完的文件。

### 4.3 调度器

完成回调只做“落库 + 唤醒调度器”，不直接调模型。调度器在被唤醒或定时巡检时，对每个空闲 actor：

1. 收集待处理输入：用户消息优先，其次是发给它的事件。先依据明确引用或有效关系确定负责根 Task；背景分发与接单判断分别记录，不能因已订阅就跳过全部准入判断。
2. 逐项检查门禁：委托已取消 → 事件归档；`auto_continue` 为 paused → 保留不唤醒；有待用户授权 → 不自动续接；续接预算用尽 → 暂停并说明；事件 run 版本过期 → 归档为历史。
3. 按 `wait_spec` 判断时机：默认等本阶段要求的子任务到齐（all）；失败、需用户处理、超过截止时间则提前唤醒。
4. 在一个事务里插入新 Run 并把选中的事件标为 claimed。数据库唯一约束保证每个 actor 只有一个活动 Run，并发完成只会成功启动一轮。
5. Run 结束时，仅对有实际模型请求纳入记录的事件更新 consumed；未纳入的退回 pending，发送状态不明的保留 unknown 并恢复核对。consumed 不等于该要求已完成；未解决的 follow-up 仍须继续、持久等待或明确交回。结束后立即重新检查，不靠后续事件触发。
6. 合并有上限（条数和等待时长），不无限凑批。

崩溃恢复：服务启动时，`claimed` 但所属 Run 已不存在的事件退回 `pending`；状态为 running 的 Run 标为 interrupted，并给负责人发 `interrupted` 事件。只读或草稿写入的 Worker 任务可以按配置开新 Run 重做，外部副作用工具留到 M2 以后，按 operation 规则处理。

以上 input_usage / disposition 等为已确认增量；详细责任转移、输入证据及恢复规则见 [融合方案第 4–7 节](deepseek-integration-review.md#4-后续输入谁接是否看过是否做完)。当前草稿未达到这些契约。

### 4.4 续接输入

自动续接的 Run 以系统事件作为输入，不伪装成用户消息。内容包括：继续原因、各事件的任务摘要与产物引用、未完成项、当前需求版本、剩余预算与授权。Worker 报告整体作为数据嵌入，其中“用户已同意”之类的文字不产生授权。

## 5. 停止与取消契约

| 操作 | 总控 Run | Worker | `auto_continue` | 用户看到 |
|---|---|---|---|---|
| 正常结束本轮 | 结束（WAIT） | 继续 | 不变 | “后台还有 N 项” |
| 停止当前回复（默认停止键） | 取消 | 继续 | → paused | “当前回复已停止，N 项后台工作仍在进行，完成后暂不自动继续” |
| 停止某个任务 | 不受影响 | 只停该任务的活动 Run | 不变；负责人收到 `stopped` 事件 | 该任务“正在停止”→“已停止” |
| 取消整项委托（含“全部停掉”“不做了”） | 取消 | 全部取消 | → forbidden | 已完成部分和取消记录保留 |
| 明确继续（点继续，或发属于该根 Task 的任意新消息；归属细化已确认） | — | — | 目标 Task 的 paused → on | 新消息和待处理结果一起起一轮 |

- 停止请求带目标 `run_id`，只作用于那一次 Run。旧 Run 的停止回执不会关闭新 Run。
- 状态分“已请求 → 正在停止 → 已停止”。只有 asyncio 任务真正退出、Run 记录写好终态后才显示“已停止”。
- 进程内停止可靠；正在进行的模型请求在客户端中止，提供方是否继续计费如实记为未知。
- 取消不撤回已经发生的外部动作。M1 的工具都是只读或草稿写入，所以不涉及这类动作。
- 用户停止之后又发来消息，视为明确继续：paused → on，新消息与待处理结果一起进入下一轮；已取消的委托不会因此恢复（[decisions.md](decisions.md) D-D）。本次 D-H 提案明确：先归属，只恢复目标根 Task；其他任务消息、旧重放与 context 不恢复它。完成后致谢不重跑，有效补充创建关联任务，详见融合方案第 5 节。

## 6. 切片调整

| 切片 | 内容 | 过关的关键证据 |
|---|---|---|
| M0 | 交接设计原 M0（单 Bot 真实交付），调度器、`messages`、`events` 从第一天就建；收口请求输入证据、恢复与 Qwen 配置一致性，不前置完整路由/主动规则系统 | 真实模型 + 真实只读工具 + 产物和根要求验收；重复输入不重复建任务；重启后已接受任务仍在；DS01–DS03 |
| M1 | 单层 Worker 闭环：`spawn` 异步派生、总控等待、结果事件、条件续接、验收交付、停止/取消/预算/晚到补充；分 A 输入归属与控制入口、B Worker、C 持久问题与整体闭环三个增量 | 第 7 节 B01–B15 与融合方案 DS04–DS11；一条真实模型 Worker 闭环 |
| M2 | 原 M1：结束门禁扩展、Nudge、进展/停滞判断、调查记录；原 M2 余下的 handoff、review 返工、产物 base_version | 交接设计原过关条件 |
| M3–M5 | 阶段目标不变；准入、权限和持续使用的协议增量见融合方案 | 按对应阶段验证，当前未运行 |

打榜接口：调度器提供 `run_until_quiescent()`，在没有待处理事件、也没有活动 Run 时返回。测试用它配合假时钟和本地模拟执行器，复现时序问题；τ-bench 适配器以后也用它把异步流程驱动成同步回合。

DeepSeek 研究的旧排序不适用：M1-A/B/C 都属于 M1，Worker 不移回 M2。新增验收为本次自拟、全部未运行；不是研究包中尚未上传的原 30 个场景。完整阶段映射见 [融合方案第 8 节](deepseek-integration-review.md#8-实施顺序与过关条件)。

## 7. M1 测试清单（均未实现、未运行）

用可控的模拟模型和模拟 Worker 测时序，不靠真实模型延迟制造竞争；真实模型只用来跑一个端到端样例。

| 编号 | 场景 | 来源 |
|---|---|---|
| B01 | 总控结束本轮、Worker 未完成：Worker 继续，不提前报完成 | Codex 场景 1 |
| B02 | 两个必要 Worker 同时完成：最多启动一轮，结果都入库、只交付一次 | Codex 场景 2、交接 B 组 |
| B03 | 续接中途又到一个结果：不丢，本轮结束后重新检查 | Codex 场景 3 |
| B04 | 停止当前回复后 Worker 完成：结果保存，不自动续接；用户点继续或发属于该根 Task 的任意新消息后才恢复，且新一轮带上该结果；其他任务不受影响 | Codex 场景 4、D-D；归属细化 D-H 已确认 |
| B05 | 停止旧 Run 时新 Run 已启动：旧回执不关闭新 Run | Codex 场景 5 |
| B06 | 停止单个 Worker：其他任务继续，确认终态后才显示已停止 | Codex 场景 6 |
| B07 | 取消委托后迟到结果：存为历史，不唤醒 | Codex 场景 7、交接 T30 思路 |
| B08 | 用户补充与完成事件同时到达：按最新需求版本解释结果 | Codex 场景 9、交接 T07 |
| B09 | 完成事件重复投递：状态、消费、交付都不重复 | Codex 场景 10、交接 T02 |
| B10 | 结果落库后、续接前崩溃：重启后找回并续接；已认领未消费的退回待处理 | Codex 场景 11、交接 T03 |
| B11 | 达到续接预算：停止扩展，保留结果并说明怎么恢复 | Codex 场景 15、交接 T34 |
| B12 | 同一任务恢复后第二次完成：新 Run 的结果正常消费 | Codex 场景 19 |
| B13 | 补充消息晚于本轮最后一步：结果标“未采用该补充”，消息保留并按开关安排下一轮 | Codex 场景 22 |
| B14 | 合并等待期间后续事件不再到来：已到结果在上限内得到处理 | Codex 场景 23 |
| B15 | Worker 报告里含“用户已同意”：不产生授权 | 交接 T23 |

本期标为不适用、不测：前台转后台的两个竞争场景（D-B）、等待子工作与结果接收者转交（单层无子工作）、后台常驻预览服务、断线停止（客户端只是读写本地数据库，没有执行端断线）、系统通知。界面上“不承诺单任务停止”的场景也不适用，因为 M1 支持单任务停止。
