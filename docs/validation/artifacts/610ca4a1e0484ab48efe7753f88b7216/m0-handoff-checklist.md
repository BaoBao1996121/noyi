# Personal Agent M0 开工与验收清单

> 本文档用于交接给下一位开发者，作为 M0 阶段的开工指引与验收标准。所有引用均来自已确认的设计文档，**不代表任何功能已实现或测试通过**。当前代码状态以 [STATUS](../STATUS.md) 为准 [plan]。

## 一、核心原则

### 1.1 设计不等于实现

- **所有设计文档仅表示"应当怎么做"，不表示"已经做到"** [plan][decisions][integration]。
- 本地已有 `src/personal_agent/` 等未提交 M0 草稿，但**未完成测试或真实闭环** [plan]。
- 函数存在、字段定义、表结构草稿**不能作为能力完成的证据** [integration]。
- 验收必须基于**实际运行结果**，而非代码静态检查。

### 1.2 模型配置

- 模型使用 **Qwen 官方云端服务**，具体型号为 **qwen3.7-plus** [decisions][integration]。
- 模型选型记录见 [模型配置](model-configuration.md)，但**尚未验证** [integration]。

---

## 二、M0 必须交付的内容与证据留存

### 2.1 M0 范围定义

M0 = 单 Bot 真实交付 + 基础设施 [plan]。具体包括：

| 交付项 | 说明 | 过关关键证据 |
|--------|------|-------------|
| 持久收件与去重 | `messages` 表含 `client_nonce` 防重 [plan] | 重复输入不重复建任务 |
| 事件系统 | `events` 表含 `event_id`、`kind`、消费状态 [plan] | 完成事件重复投递不重复处理 |
| 调度器 | 单进程 asyncio，SQLite WAL [plan] | 重启后已接受任务仍在 |
| 真实模型接入 | Qwen qwen3.7-plus 真实调用 [plan][integration] | 一条真实模型任务的完整闭环证据 |
| 真实只读工具 | 至少一个实际可用工具 [plan] | 工具调用产生可验证产物 |
| 请求输入证据 | `input_usage` 表记录每次请求纳入的输入 [plan][integration] | 能追到实际组装提交的输入 |
| 恢复边界 | 崩溃后可找回并续接 [plan][integration] | DS01–DS03 场景验证 |

### 2.2 M0 验收检查步骤

#### 检查点 1：真实模型闭环

```
□ 启动 pa serve 常驻服务
□ 通过 CLI 提交一个需要模型处理的根 Task
□ 观察数据库 messages/events/runs 表的实际写入
□ 验证产物文件确实生成且内容符合预期
□ 验证根 Task 的验收状态正确更新
□ 证据留存：截图或导出相关数据库记录、产物文件哈希
```

**注意**：不能使用模拟模型代替真实模型 [plan]。

#### 检查点 2：重复输入防重

```
□ 发送两条完全相同的用户消息（相同 client_nonce）
□ 验证只创建了一个 Task
□ 证据留存：messages 表中 client_nonce 唯一性检查
```

#### 检查点 3：崩溃恢复

```
□ 在 claim 后、模型请求前强制停止 pa serve
□ 重启服务，验证输入仍保留（DS01）[integration]
□ 在请求中途崩溃，验证请求状态可为 unknown（DS02）[integration]
□ 在交付写出后、确认前崩溃，验证同一 delivery_id 恢复（DS03）[integration]
□ 证据留存：崩溃前后数据库快照对比
```

#### 检查点 4：输入证据分层

验证三层输入事实是否分别留证 [integration]：

```
□ 第一层：已持久保存（messages 表有记录）
□ 第二层：已进入某次模型请求（input_usage 表有记录）
□ 第三层：责任已处置（followup_dispositions 表有 handled 状态）
```

**关键**：`adopted_by_run` 仅作兼容摘要，**不作最终完成证据** [plan][integration]。

#### 检查点 5：Qwen 配置一致性

```
□ 检查 model-configuration.md 中记录的型号是否为 qwen3.7-plus
□ 验证实际 API 调用使用的模型参数与配置一致
□ 证据留存：API 请求日志
```

---

## 三、M1 Worker 与 M2 Nudge 的先后顺序

### 3.1 顺序调整决定

根据决策 D-A [decisions]：

> **单层 Worker 闭环提到 M1，原 M1（Nudge、进展判断）顺延到 M2**

**原因**：用户要求优先打通单层 Worker 的完整闭环；后台续接是 Nudge 的前置 [decisions]。

### 3.2 各阶段范围

| 阶段 | 内容 |
|------|------|
| **M0** | 单 Bot 真实交付，调度器、`messages`、`events` 从第一天就建 [plan] |
| **M1** | 单层 Worker 闭环：`spawn` 异步派生、总控等待、结果事件、条件续接、验收交付、停止/取消/预算 [plan] |
| **M2** | 结束门禁扩展、**Nudge**、进展/停滞判断、调查记录 [plan] |

### 3.3 M1 内部增量顺序

M1 分为三个可审查增量 [integration]：

1. **M1-A**：输入归属与控制入口
2. **M1-B**：Worker 闭环
3. **M1-C**：持久问题与整体闭环

**重要**：M1-A/B/C 是同一里程碑内的增量，**不把 Worker 再推迟到 M2** [integration]。

### 3.4 验证顺序

```
M0 收口 → M1-A → M1-B → M1-C → M2
```

---

## 四、输入处理的三个阶段区分

根据融合方案 D-G 决定 [decisions][integration]，输入处理分为三个独立阶段：

### 4.1 第一阶段：收到输入（持久保存）

**含义**：消息/事件已写入数据库 [plan][integration]。

**证据**：`messages` 或 `events` 表中有记录 [plan]。

**注意**：保存成功**不等于**已被模型看到 [integration]。

### 4.2 第二阶段：进入模型请求（InputUsage 记录）

**含义**：该输入被纳入某次 Run 的模型请求中，发送给 qwen3.7-plus [plan][integration]。

**证据**：`input_usage` 表中有记录 [plan][integration]。

**关键区分**：
- **认领（claimed）≠ 使用**：claimed 只表示准备处理 [plan][integration]。
- **consumed ≠ 责任已完成**：consumed 仅表示已纳入模型请求 [plan]。

### 4.3 第三阶段：要求落实（FollowupDisposition 记录）

**含义**：该输入对应的责任已被处置 [integration]。

**证据**：`followup_dispositions` 表中有 handled 状态及处置依据 [integration]。

**关键规则**：
- **入队、模型回话、Run 结束、events.consumed 都不自动满足 handled 条件** [integration]。
- **尚有未处理的责任项时，Task 不能悄悄成功** [integration]。
- **模型读过补充但未落实 → 根 Task 不自动成功**（DS07）[integration]。

### 4.4 三层关系总结

```
收到输入（持久保存）→ 进入模型请求（InputUsage）→ 要求落实（FollowupDisposition handled）
```

**每一层都是必要条件，但不是充分条件** [integration]。

---

## 五、暂停 / 取消 / 完成后补充的差别

根据决策 D-D 和停止与取消契约 [plan][decisions]：

### 5.1 暂停（paused）

**触发**：停止当前回复 / 预算用尽 / `auto_continue` 设为 paused [plan]。

**行为**：
- 总控 Run 取消，**Worker 继续执行** [plan]
- 结果保存，**不自动续接** [plan]

**恢复**：
- **用户发来属于该根 Task 的新消息** → `paused → on` [plan][decisions]
- **不需要**特定词汇 [integration]

**关键**：另一项任务的消息**不恢复它** [integration]。

### 5.2 取消（forbidden）

**触发**：明确取消整项委托 / `auto_continue` 设为 forbidden [plan]。

**行为**：
- 总控 Run 取消，**全部 Worker 取消** [plan]
- 迟到结果存为历史，**不唤醒** [plan]

**恢复**：
- **普通消息和旧结果不能复活** [integration]
- 需**建新根 Task** [integration]
- 已取消的委托**不会因用户发新消息而恢复** [plan][decisions]

**关键**：取消是终态，不可逆 [plan]。

### 5.3 完成后补充

**两种情况**：

- **致谢消息**：无需重做，零新工作 [integration]。
- **有效补充**：创建一个关联的后续根 Task，引用 `prior_task_id` [integration]。

**关键**：
- **完成后的闲聊不重跑** [integration]
- **有效补充创建关联任务** [plan][integration]

### 5.4 三种状态对比表

| 维度 | 暂停（paused） | 取消（forbidden） | 完成后补充 |
|------|---------------|-------------------|-----------|
| **触发** | 停止当前回复 / 预算用尽 | 明确取消委托 | 任务已成功交付 |
| **Worker 状态** | 继续执行 | 全部取消 | 已结束 |
| **待处理结果** | 保留 | 存为历史，不唤醒 | 不适用 |
| **恢复方式** | 发属于该任务的新消息即恢复 | 不能恢复，需新建 Task | 致谢不处理；有效补充新建关联 Task |
| **auto_continue** | paused | forbidden | 不变 |
| **数据来源** | [plan] 第 5 节、[decisions] D-D | [plan] 第 5 节、[decisions] D-D | [integration] 第 5 节、DS09 |

### 5.5 验收检查步骤

#### 检查点：暂停恢复

```
□ 启动一个带 Worker 的根 Task
□ 点击"停止当前回复"
□ 验证 auto_continue 变为 paused
□ 验证 Worker 继续执行并完成
□ 发送一条属于该根 Task 的新消息
□ 验证 auto_continue 恢复为 on
□ 证据留存：auto_continue 字段变化日志
```

#### 检查点：取消不可恢复

```
□ 启动一个根 Task
□ 点击"取消整项委托"
□ 验证 auto_continue 变为 forbidden
□ 发送一条普通消息
□ 验证原 Task 未恢复
□ 证据留存：cancel_state 字段
```

#### 检查点：完成后补充

```
□ 完成一个根 Task
□ 发送一条致谢消息
□ 验证未创建新 Task
□ 发送一条有明确新要求的消息
□ 验证创建了一个关联根 Task，引用 prior_task_id
□ 证据留存：tasks 表计数
```

---

## 六、M0 开工前必须确认的事项

### 6.1 环境准备

```
□ SQLite WAL 模式已启用
□ pa serve 常驻服务可启动
□ CLI 客户端可用
□ Qwen API 密钥已配置
□ 数据库迁移脚本已执行
```

### 6.2 代码状态核实

```
□ 查看 STATUS.md 了解当前代码实际完成情况
□ 确认哪些模块仍是草稿状态
```

### 6.3 测试资源准备

```
□ 准备可控的模拟模型用于时序测试 [plan]
□ 准备真实 qwen3.7-plus API 访问权限
```

---

## 七、附录：M1 测试清单参考

以下场景来自 [plan] 第 7 节，供 M1 阶段参考（均未实现）：

| 编号 | 场景 | 关键点 |
|------|------|--------|
| B01 | 总控结束本轮、Worker 未完成 | Worker 继续 |
| B02 | 两个必要 Worker 同时完成 | 最多启动一轮 |
| B04 | 停止当前回复后 Worker 完成 | 不自动续接，用户发新消息才恢复 |
| B07 | 取消委托后迟到结果 | 存为历史，不唤醒 |
| B10 | 结果落库后、续接前崩溃 | 重启后找回并续接 |
| B11 | 达到续接预算 | 停止扩展，保留结果 |
| B15 | Worker 报告里含"用户已同意" | 不产生授权 |

新增验收规格 DS01–DS16 来自 [integration] 第 9 节，均为 `specified_not_run` [integration]。

---

**最后提醒**：本文档所有内容均为设计意图，**不代表已实现或测试通过**。验收必须基于实际运行结果 [plan][decisions][integration]。