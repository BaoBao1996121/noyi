# 当前模块替换接口

核心实现和新增能力均可扩展；当前采用明确的 Python 接口和组合注入。不声称能直接安装任意 Pi extension 或 DSH 插件。

## 能力插件（2026-11-05 起）

模型能调用的每个工具都由插件提供，内置工具也一样：`core.artifacts`（读资料、写/读产物、提交验收）、`core.orchestration`（委派、等待、提问、复盘等）、`core.following`（持续跟进）。见 `src/personal_agent/plugins.py` 与 `builtin_plugins.py`。

新增能力不改核心代码，写一个模块并在配置里登记：

```python
# my_notes.py
from personal_agent.plugins import Plugin, ToolSpec
from personal_agent.tools import tool

def plugin():
    lookup = tool("lookup_note", "Look up a note by key.", {"key": {"type": "string"}}, ["key"])
    return Plugin("notes", "0.1", [ToolSpec(lookup, lambda call, args: {"note": "..."}, effect="read")],
                  prompt="Use lookup_note for stored notes.")
```

```toml
[plugins]
modules = ["my_notes"]
```

- 每个工具声明副作用类别 `effect`：`read` / `local_write` / `external_retryable` / `external_unknown`。外部副作用的调用如果因崩溃中断，恢复时会标成“结果未知，先查证再重试”，不会被盲目重放。
- 插件工具和内置工具走同一套流程：参数校验、预算、取消检查、操作落库、恢复。插件不能完成或验收任务；`core.` 前缀和已有工具名保留给内置插件，不能覆盖。
- 插件的 `prompt` 片段追加到系统提示末尾。
- 工具可声明 `available(runtime, run)`：不适用于当前任务的工具不会出现在模型的工具列表里，模型硬调用也只让这一次调用失败。
- 插件代码意外抛错只让这一次工具调用失败，不会让整个任务失败。
- 插件是本机安装的可信代码，不是安全隔离；调用不可信的外部程序仍需另做进程/权限隔离。

系统提示按任务拼装（`Runtime.system_prompt`）：核心规则 `SYSTEM` 总是发送；委派、持续跟进、目标、回顾四段说明只在对应工具可用或任务属于目标时追加。普通任务看到 19 个工具、约 7.9K 字符系统提示。

对话中由代码负责的话（「已开跑」、20 分钟进度、交付格式、回执检查、明确「记住」）集中在 `speech.py`；模型写的回执和交付摘要都先经过这里检查。

模型可以走本机模型网关（如 Magpie，`base_url` 为 `http://127.0.0.1:<端口>/v1`，只接受本机回环地址）。走网关时按角色传档位：回复用户的接收层用 `chat_effort`，干活用 `effort`；网关上的 Claude 不支持强制工具调用，接收层没给出决定时由原有的重问逻辑处理。官方千问不传档位，接收层仍强制调用。

联网能力是插件 `web_tools.py`（`web_search`、`fetch_page`），环境里有 `DASHSCOPE_API_KEY` 时自动启用。可在配置文件 `[web]` 节改 `search_url`、`search_model`、`api_key_env`、`task_search_limit`（默认每个任务 30 次）、`daily_search_limit`（默认每天 200 次）、`timeout`。搜索后端是 `BailianSearch`，替换时实现同样的 `search(query)` 返回 `urls / summary / billed_searches`。读网页只访问公网地址（每一跳重定向都检查），正文清洗后存在任务目录 `web/` 下，按页返回。交付里引用 `[page_id]` 必须对应本任务成功读过的页，否则验收不通过。

通过验收的成果可在 `/r/{task_id}` 打开交付页：服务端渲染、无脚本、单独的严格 CSP；引用链接只取自账本里真实读过的网页，正文里模型写的链接不生效。

管理类工具（`record_goal_progress`、`propose_policy_adjustment`、`review_policy_proposal`、`record_goal_review`、`record_investigation`、`handoff_task`、`observe_following`）对模型隐藏（`builtin_plugins.HIDDEN_TOOLS`）。目标进度改由账本在任务结束时写 `ledger_progress` 事件；回顾是普通报告任务：先用 `read_review_evidence` 读冻结证据，再写报告提交，不再要求模型填结构化评估。对应 store 方法仍保留，供以后的程序化入口使用。

| 边界 | 当前实现与替换方式 | 保持不变的约束 |
|---|---|---|
| 执行循环 | `AgentEngine.run`，默认 `PiEngine`；传入 `Service(engine=...)` | 只能通过受管回调请求模型和工具；不能自行完成任务或增加权限 |
| 模型 | `Model.complete/close`，默认 `QwenModel`；传入 `Service(model=...)` | 请求和用量计入持久预算；无隐藏重试 |
| 来源读取 | `SourceReader.read`，默认精确文件/HTTP 来源读取；`source_reader=...` | 只读取已授权描述符；结果仍受大小、快照、哈希和引用检查 |
| 投递 | `DeliveryChannel.send`，默认本地文件收件箱；`channel=...` | 按 delivery ID 幂等，明确接受回执；接受不等于人已阅读 |
| 记忆值加载 | `MemoryProvider.load`；注入 Store | 成员、集合、版本、删除和用途由账本控制，加载器不能绕过受众边界 |
| 变化观察 | `ObservationSource.observe`；`Service(observer=..., clock=...)` | 观察器不能注册策略或启动任务；修订、游标、去重和准入由运行时控制 |
| 新工具 | 写插件模块并在 `[plugins]` 登记（见上节）；`Service(plugins=...)` 可直接注入 | 所有副作用先记意图与回执；新证据需定义“新进展”判定，不能靠重复成功绕过停滞限制 |

接口见 `src/personal_agent/ports.py`，模型合同见 `contracts.py`。执行器、来源和投递替换在 `tests/test_m1_scenarios.py` 中实际验证，记忆替换在 `tests/test_m3.py` 中验证，观察器在 M4 测试中注入。M5 的新证据读取也接入了唯一进展记录：第一次读到一个冻结证据页是新进展，重复读同一页不是。

任务账本目前使用 SQLite，是一致性和授权的权威。更换持久化后端需要实现相同事务/版本语义并迁移数据；不是改一个类名即可任意热切。内核可替换与外围可扩展都成立，但状态协议本身需要保持稳定。

未来可在这些边界外增加 Cordis/DSH 或 Pi extension 适配层，将它们的生命周期、服务和工具能力映射进本系统。第三方模块需要声明能力范围、配置、版本、停止与恢复行为，并经过合同测试；进程内可信扩展不等于安全隔离。生态适配器属于后续增量，不是当前可用性闭环的依赖。
