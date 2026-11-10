"""Delivery page (decision X-2): one stable link per task, latest accepted version.

Server-rendered HTML without scripts, so it works under the console's CSP and
can be opened, bookmarked or shared on the same machine. Only accepted,
digest-checked artifacts are shown; drafts never appear here.
"""
from html import escape
import json
import re

from .contracts import digest
from .tools import MAX_SOURCE_BYTES, Toolset

STYLE = """body{font:16px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f6f5f2;color:#222}
main{max-width:760px;margin:0 auto;padding:32px 20px 64px}header{color:#666;font-size:14px;margin-bottom:20px}
article{background:#fff;border-radius:12px;padding:28px 32px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
h1,h2,h3{line-height:1.35}h1{font-size:24px}h2{font-size:20px;margin-top:28px}h3{font-size:17px}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #e3e1dc;padding:6px 8px;text-align:left}
code,pre{background:#f1f0ec;border-radius:4px}pre{padding:12px;overflow:auto}a{color:#2458c5}
.cite{font-size:12px;vertical-align:super}.sources{margin-top:28px;font-size:14px;color:#555}.muted{color:#888}"""


def _inline(text, links):
    html = escape(text)
    html = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"`([^`]+)`", r"<code>\1</code>", html)
    def cite(match):
        sid = match.group(1)
        url = links.get(sid)
        return f'<a class="cite" href="{escape(url)}" rel="noreferrer noopener" target="_blank">[{escape(sid)}]</a>' if url else f'<span class="cite">[{escape(sid)}]</span>'
    # "[id](url)" from the model: keep the citation, the link comes from the ledger.
    html = re.sub(r"\[([A-Za-z0-9_\-]{1,40})\]\(https?://[^)\s]+\)", r"[\1]", html)
    return re.sub(r"\[([A-Za-z0-9_\-]{1,40})\]", cite, html)


def markdown(text, links):
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i]); i += 1
            out.append("<pre><code>" + escape("\n".join(block)) + "</code></pre>")
        elif "|" in line and i + 1 < len(lines) and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*", lines[i + 1]):
            cells = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]
            head, rows = cells(line), []
            i += 2
            while i < len(lines) and "|" in lines[i]:
                rows.append(cells(lines[i])); i += 1
            i -= 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c, links)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c, links)}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")
        elif m := re.match(r"(#{1,3}) (.*)", line):
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2), links)}</h{level}>")
        elif re.match(r"\s*[-*] ", line):
            items = []
            while i < len(lines) and re.match(r"\s*[-*] ", lines[i]):
                items.append("<li>" + _inline(re.sub(r"^\s*[-*] ", "", lines[i]), links) + "</li>"); i += 1
            i -= 1
            out.append("<ul>" + "".join(items) + "</ul>")
        elif line.strip():
            out.append("<p>" + _inline(line, links) + "</p>")
        i += 1
    return "\n".join(out)


def source_links(store, task):
    """Citation targets: fetched web pages and authorized HTTP sources of this task tree."""
    links = {s["id"]: s["location"] for s in task["spec"]["sources"] if s.get("kind") == "http"}
    for op in store.all("SELECT o.result FROM operations o JOIN tasks t ON t.id=o.task_id WHERE t.root_task_id=? AND o.tool='fetch_page' AND o.status='succeeded'", (task["root_task_id"],)):
        result = json.loads(op["result"] or "{}")
        if result.get("page_id") and str(result.get("url", "")).startswith(("http://", "https://")):
            links[result["page_id"]] = result["url"]
    return links


def render(store, data, task_id):
    task = store.task(task_id)
    row = store.one("SELECT * FROM deliveries WHERE task_id=? AND artifact IS NOT NULL ORDER BY rowid DESC LIMIT 1", (task_id,))
    if not row or task["acceptance"] != "passed":
        return None
    artifact = json.loads(row["artifact"])
    path = Toolset(data, task).artifact_path(artifact["ref"])
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("产物超过限制")
    raw = path.read_bytes()
    if digest(raw) != artifact["sha256"]:
        raise ValueError("产物发生变化，无法作为已验收版本提供")
    links = source_links(store, task)
    text = raw.decode("utf-8")
    title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("#")), task["spec"]["objective"][:40])
    cited = [sid for sid in dict.fromkeys(re.findall(r"\[([A-Za-z0-9_\-]{1,40})\]", text)) if sid in links]
    versions = store.one("SELECT COUNT(*) n FROM deliveries WHERE task_id=? AND artifact IS NOT NULL", (task_id,))["n"]
    sources = ("<div class=\"sources\"><strong>来源</strong><ol>" + "".join(
        f'<li>[{escape(s)}] <a href="{escape(links[s])}" rel="noreferrer noopener" target="_blank">{escape(links[s])}</a></li>' for s in cited) + "</ol></div>") if cited else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{STYLE}</style></head><body><main>
<header>Noyi 交付 · 第 {versions} 版 · 已验收 · <span class="muted">sha256 {artifact["sha256"][:12]}</span></header>
<article>{markdown(text, links)}{sources}</article></main></body></html>"""
