# 在线演示站（GitHub Pages）

地址：<https://baobao1996121.github.io/noyi/>。落地页中英双语（按浏览器语言默认，右上角可切换），点进去是真实界面的只读版：账本回放、记忆星系、四件预置的事。页面不调用模型，没有任何写接口。

## 数据从哪来，为什么不会带出自用库

- 演示库由 `scripts/demo-replay.py build` 在 CI 的空目录里现场生成，CI 机器上没有 `.pa/personal`。
- 导出（`personal_agent/demo_site.py`）有三道检查，任何一道不过都拒绝导出、不留半成品：
  1. 库里每一次模型请求都必须是 `demo-scripted`，真实使用的库必然有真实模型，指错库会直接失败；
  2. 只经界面自己用的几个只读接口导出 JSON，不复制数据库文件；
  3. 演示库目录内的路径改成相对路径；其他任何本机路径（盘符、`/home`、`/Users` 等）出现在输出里就停下，并报出在哪个文件。
- 测试：`tests/test_demo_site.py`。

## 发布、更新与回滚

| 要做的事 | 命令 |
|---|---|
| 发布当前 main | `git tag demo-YYYYMMDD-N && git push origin demo-YYYYMMDD-N` |
| 回滚到某个好版本 | `gh workflow run pages.yml --ref <pages-ok-N 或旧的 demo-* 标签>`（必须在旧标签上运行：Pages 按运行时的提交号记部署，在 main 上运行、只换内容的话不会替换线上版本） |
| 看最近几次发布 | `gh run list -w pages.yml` |
| 看失败原因 | `gh run view <id> --log-failed` |
| 取截图和浏览器日志 | `gh run download <id>`（`smoke-before-publish`、`smoke-live`） |
| 立刻做一次外网检查 | `gh workflow run probe.yml` |

工作流 `.github/workflows/pages.yml` 的门禁依次是：相关测试 → 造演示库 → 带检查的导出 → 发布前在 CI 里用无头浏览器冒烟检查 → 发布 → 对线上地址再做一次冒烟检查。前四步任何一步失败都不会发布，线上保持原样。线上检查失败时，工作流自动重新发布上一个 `pages-ok-*` 版本（每次线上检查通过都会打这个标签）。

冒烟检查（`scripts/site-smoke/smoke.mjs`）要求：落地页和演示页没有控制台报错和失败请求，图片都能加载，英文切换生效，「60 秒看它怎么面对逾期」按钮打开逾期那件事的回放并真的在播放，演示页只读，记忆星系画出来。每一项失败都会打印原因。

演练线上失败自动回退：`gh workflow run pages.yml --ref <demo-标签> -f drill=true`（发布后故意让线上检查失败，应自动重新发布上一个 `pages-ok-*`）。

`probe.yml` 每天从 GitHub 的境外机器检查一次：证书剩余天数、`build.json`、冒烟检查。失败时 GitHub 会发邮件。

## 本地复现

```bash
python scripts/demo-replay.py build --data .pa/demo-site
python scripts/demo-replay.py export --data .pa/demo-site --out dist/site
python scripts/demo-replay.py site --out dist/site          # http://127.0.0.1:8791/noyi/
node scripts/site-smoke/smoke.mjs http://127.0.0.1:8791/noyi/ smoke-local
```

同一个提交在本地和 CI 里讲的是同样的四件事（演示时钟按 `Asia/Shanghai` 排布，CI 里也设了这个时区）。也可以用 `gh run download <id>` 把 CI 构建好的整站下载下来，在本地打开对比，判断问题出在构建还是托管。
