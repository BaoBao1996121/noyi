# Pi 核心循环接入

日期：2026-11-04。用户提出复用 Pi 的稳定核心循环、保留自定义能力；本项目据此接入 `@earendil-works/pi-agent-core@1.5.0`。验证状态以 STATUS 和运行记录为准。

## 职责

| 层 | 负责什么 |
|---|---|
| Pi Agent | 模型与工具的多轮循环、工具声明与参数验证、顺序执行、回填结果、结束事件 |
| Python 模型适配 | 千问官网 SDK 请求、凭据、持久预算预留、实际输入使用记录、请求回执 |
| Python 任务层 | SQLite Task/Run、版本、恢复、权限范围、操作记录、产物验收、Delivery |

本次复用 Pi 提供的 `streamFn` 扩展点，将模型通信交给已有 Python 适配器；不是把 Pi 当一次模型调用后继续用自写循环。旧 `Runtime.loop` 的模型/工具 while 调度已替换为 Pi；Python 只处理它发来的模型、工具与轮次结束回调。

## 进程与可信边界

`pa serve` 仍是唯一服务入口与数据库负责人。每个活动 Run 启动一个受管 Node 子进程，运行 Pi Agent；它通过标准输入输出与 Python 通信。M0 的总并发仍为一。关闭 CLI 不影响 serve；结束 serve 会取消请求、关闭子进程，已有输入和记录保留。

这把原设计的“一个 OS 进程”调整为“一个服务及受管执行子进程”，需要 Node >=22.19.0。没有新增独立常驻服务或网络监听。SQLite 仍是业务状态唯一权威，不把 Node 内存、Pi 的 agent_end 或文本“完成”当成任务成功。

只注册我们的 read_source、write_artifact、submit_result。Node 子进程不接收 API key，不启用 Pi coding-agent 的默认 shell、编辑器工具、自动扩展发现、自动压缩或自动重试。进程边界本身不是 OS 沙箱，实际权限范围仍由 Python 工具入口检查。

## 故障和升级

- 每次请求先检查当前 Run 与持久预算，再记录输入和发送意图；没有收到回复时保留未知状态。
- 每次工具执行先检查当前 Run、预算、参数和工具授权，再保存操作意图。结果先保存，再返回给 Pi。
- Pi 负责拒绝输出截断造成的不完整工具调用；本项目把针对截断的修正限制为一次，重复截断暂停。
- 产物通过验收后结束 Task；Pi 随后退出。如果 Pi 自己停止而没有验收，任务保持未通过。
- Node 异常退出不能使任务成功。服务重启先核对未决记录；本地产物按原 operation_id 对账，重试不盲重写。
- npm 依赖固定版本并提交 lockfile，安装使用 `npm ci --ignore-scripts`。升级 Pi 时重跑协议、恢复、截断、权限和真实模型验收，不跟随 main 自动升级。

## 一手依据

原 `badlogic/pi-mono` 官方仓库现跳转到 `earendil-works/pi`。核对源码版本为 `e88525506177d22b6e93029d8f6db1aab323c74a`；npm 实际接入固定为 1.5.0，具体传递依赖以 lockfile 为准。

- [Agent 文档](https://github.com/earendil-works/pi/blob/e88525506177d22b6e93029d8f6db1aab323c74a/packages/agent/README.md)：自定义工具、模型函数、事件与生命周期。
- [Agent 循环源码](https://github.com/earendil-works/pi/blob/e88525506177d22b6e93029d8f6db1aab323c74a/packages/agent/src/agent-loop.ts)：多轮循环、截断工具拒绝、finishTurn、工具模式。
- [SDK 文档](https://github.com/earendil-works/pi/blob/e88525506177d22b6e93029d8f6db1aab323c74a/packages/coding-agent/docs/sdk.md)：coding-agent 另有会话/资源/重试等职责，本次未引入。

Pi 另有实验性的 pi-durable，不能笼统认为它没有持久化能力；本轮选择保留现有任务层，避免同时更换执行循环与业务状态协议。
