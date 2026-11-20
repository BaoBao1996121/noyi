# 浏览器能力包验收预登记（PREREG）

- 登记日期：2026-11-20，写在实现和验收之前；门槛由总控定，验收后只能照实填结果，不改门槛。
- 设计：[docs/design/browser.md](../design/browser.md)

## 任务（3 个真实网站）

| 代号 | 起始页 | 要求 | 人工核对的正确结果 |
|---|---|---|---|
| search | 必应 `cn.bing.com` | 搜「playwright pypi」，打开第一条指向 pypi.org 的结果，报页面上写的最新版本号 | 打开 `pypi.org/project/playwright/`，版本号与验收当天 PyPI JSON 接口 `info.version` 一致（登记时为 1.65.2） |
| form | `httpbin.org/forms/post` | 填 7 个字段并提交，报返回的 custname 和 size | custname = Noyi Test，size = medium |
| list | `webscraper.io/test-sites/e-commerce/static/computers/laptops` | 从第 1 页点翻页按钮翻到第 3 页，数第 3 页上价格低于 $600 的笔记本有几台，列出名字 | 4 台：HP 350 G1、Asus VivoBook…、Dell Vostro 15、Acer Spin 5（2026-11-20 人工核对） |

第一阶段用的 quotes.toscrape.com（重启后连接被重置）和 github.com（连接超时）这台机器现在都连不上，所以搜索和翻页两项换站；截图组也在新站上重跑，两组比同一个站。

## 两层测法

1. **对照层（同一套脚本）**：`scripts/run-browser-check.py --mode compare`。同一个精简提示、同一个 deepseek-flash（思考关闭），一组用 Noyi 的浏览器工具（`personal_agent.browser` 的真实实现），一组用截图循环（动作后固定等 2 秒再截图，computer-use 参考实现的做法）。只比两种看网页的方式，不含 Noyi 自己的系统提示和上下文。
2. **整链路层**：`scripts/run-browser-check.py --mode e2e`。把任务当成用户的一句话交给 Noyi（接收层 → 执行层 → 加载浏览器能力包 → 交付验收），计交付和正确率。

两层都：每个站跑 5 次；浏览器在每组开始时启动一次（常驻）；模型请求遇到连接中断、超时、429、5xx 重试一次，重试计入耗时和 token；每次结果写一行到输出目录的 `results.jsonl`，跑一半中断可以从已有行数续跑。

## 门槛（全部要过）

| # | 门槛 | 在哪层测 | 怎么算 |
|---|---|---|---|
| G1 | 交付不少于 14/15 | 整链路 | 任务以「验收通过并送达」结束算交付 |
| G2 | 计数题和人工核对一致，错的不超过 1/5 | 整链路 | list 任务 5 次里，台数不是 4 或名字缺漏/多出的算错 |
| G3 | 输入 token 不超过截图组 | 对照层 | 每个站分别比：Noyi 浏览器组 5 次输入 token 中位数 ≤ 截图组 5 次中位数。三个站都要满足 |
| G4 | 每轮浏览器侧耗时 p50 ≤ 800 ms | 对照层和整链路都报，以整链路为准 | 每次 `browser_*` 工具调用里浏览器自己花的时间（动作 + 等稳定 + 取快照，含页面加载），所有调用的中位数 |
| G5 | 一次真实登录交接 | 人工 | 用户在手机上打开实时画面亲手登录，点「我登好了」或自动检测到登录完成后，任务自动接着做完并交付。Noyi 全程不经手密码。提前通知总控约用户 |

另外照实报告、不作门槛：每个任务的总耗时中位数、模型轮数、截图组同口径数字、交付里正确但答得啰嗦的情况。

## 结果

（验收后填）
