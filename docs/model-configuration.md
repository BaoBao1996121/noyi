# 模型配置与选型记录

日期：2026-11-04。服务方向由用户明确指定为 Qwen 官网；型号由开发代理选择。默认代码、示例和真实请求统一使用下表。实际验证结果见 [M0 验收记录](validation/M0_REPORT.md)。

| 项目 | 选择 / 当前约定 |
|---|---|
| 提供方 | 千问 AI 平台（Qwen 官网，中国站） |
| 首选 API 模型 ID | `qwen3.7-plus` |
| 接口 | OpenAI 兼容 Chat Completions，通过一个 Python SDK 适配 |
| 默认基础地址 | `https://maas.qianwenaiapi.com/compatible-mode/v1` |
| 凭据环境变量 | `DASHSCOPE_API_KEY`；每台机器使用自己的凭据 |
| 显式覆盖 | `PA_MODEL`、`PA_BASE_URL`；正式验证前核对账号地域与业务空间 |
| 默认调用预算 | `enable_thinking=false`、输出上限 4096、单次超时 90 秒；已用于本轮真实任务，长输出截断及质量限制见验收报告 |

选择理由：M0 首要验证文本任务、多轮工具调用和结果交付。官方 [Function Calling 文档](https://help.aliyun.com/zh/model-studio/qwen-function-calling) 列出 Qwen3.7-Plus 系列支持工具调用；据此先用一个通用 Plus 模型建立基线。此决定不声称它在本项目评测中胜过其他模型，也不静默切换到本地模型、第三方代理或 DeepSeek。

## 官网地址与兼容配置

已核对千问官网 [获取 API Key](https://platform.qianwenai.com/docs/api-reference/preparation/api-key) 与 [首次调用](https://platform.qianwenai.com/docs/developer-guides/getting-started/first-api-call)：标准 API 使用 `https://maas.qianwenaiapi.com/compatible-mode/v1`，文档列出的模型 ID 包括 `qwen3.7-plus`，Python 示例也使用 `DASHSCOPE_API_KEY` 环境变量。

先前草稿曾误把百炼旧地址当作用户所指的官网地址，同时示例写 `qwen-plus`、代码默认写 `qwen3.7-plus`。现已统一为上表，**不会因请求失败自动换平台、地址或型号**。用户在别的电脑上应配置该平台自己的 key；Token Plan、国际站或百炼账号要显式使用与自身账号匹配的配置，不能假设凭据通用。

适配器仅允许明确的官方 HTTPS 主机与兼容 API 路径，不跟随 HTTP 重定向，不启用 SDK 隐藏重试。保留百炼官方地址作为显式兼容选项；国际 QwenCloud 目前未验证，不能当作默认可用。

官方 [思考模式文档](https://help.aliyun.com/zh/model-studio/deep-thinking) 说明 Qwen3.7-Plus 的思考开关。当前非思考配置用于先验证工具与产物闭环，能力与质量以本项目真实任务为准。

## 真实验证应留存的证据

- 请求实际采用的 model、提供方返回的 model/版本信息（若有）、非敏感 endpoint、思考开关和预算。
- 能获取时记录 request ID、用量、耗时与调用失败；缺失费用数据明确标未知，不编费用。
- 一条真实任务的工具参数与受控回执、资料读取覆盖、产物、根要求验收和投递结果。简单问候成功不能替代工具闭环。
- `qwen3.7-plus` 作为服务模型 ID 不等于不可变快照；评测记录日期和返回版本，必要时再选官方可用的固定版本。

仓库只保存环境变量名和无密钥示例，不包含本机凭据。其他电脑的账号和地域配置未验证。后续接手者先看 [STATUS](../STATUS.md) 与具体运行证据，不将单台机器一次成功外推成全部账号可用。
