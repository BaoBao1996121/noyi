"""Start/stop/status the local WebUI without a visible service console (Windows)."""
import argparse
import codecs
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
# Bootstrap titles of this app; the old name still matches while an older
# running service is being replaced.
SERVICE_TITLES = ('Noyi', 'Personal Agent')


def _ensure_utf8_console():
    """Rewrap stdout/stderr as UTF-8 so Chinese text renders in cmd/PowerShell/Git Bash alike.

    Windows inherits the console's active code page (often GBK, cp936) as
    ``sys.stdout.encoding``; ``PYTHONUTF8=1`` only changes Python's internal
    default encoding, not the stream already bound to that code page. Forcing
    UTF-8 here, with ``errors='replace'`` as a last-resort safety net, is a
    mechanism-level fix that applies to every subcommand and every caller
    (cmd.exe, PowerShell, Git Bash) instead of patching individual prints.
    """
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name)
        if stream is None:
            continue
        current_encoding = getattr(stream, 'encoding', None)
        if current_encoding and codecs.lookup(current_encoding).name == 'utf-8':
            continue
        try:
            wrapped = io.TextIOWrapper(
                stream.buffer, encoding='utf-8', errors='replace', newline='', line_buffering=True
            )
        except AttributeError:
            continue
        setattr(sys, stream_name, wrapped)


_ensure_utf8_console()


def request(url, body=None, token=None):
    headers = {'X-PA-Token': token} if token else {}
    req = urllib.request.Request(url, data=b'{}' if body is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=2) as response:
        return json.load(response)


def status(url):
    try:
        return request(url + '/api/bootstrap')
    except (OSError, ValueError):
        return None


def describe_status(url, marker):
    """Return a structured status description without printing anything.

    Used by both the ``status`` subcommand and tests, so the decision logic
    (running / stopped / stale marker / conflicting instance) has one owner.
    """
    saved = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
    current = status(url)
    if current is None:
        return {'running': False, 'url': url, 'pid': None, 'stale_marker': saved is not None}
    matches_marker = bool(saved) and current.get('instance_id') == saved.get('instance_id')
    return {
        'running': True,
        'url': url,
        'pid': saved.get('pid') if matches_marker else None,
        'stale_marker': bool(saved) and not matches_marker,
    }


def run_status(args):
    data = args.data.resolve()
    marker = data / 'desktop-process.json'
    url = f'http://127.0.0.1:{args.port}'
    info = describe_status(url, marker)
    if info['running']:
        if info['pid']:
            print(f"Noyi 正在运行：{info['url']}（进程号 {info['pid']}）")
        else:
            print(f"Noyi 正在运行：{info['url']}（进程号未知，可能由其他方式启动）")
    else:
        print('Noyi 当前未运行。')
        if info['stale_marker']:
            print('提示：发现残留的进程记录，可直接重新启动。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status', 'restart'])
    parser.add_argument('--data', type=Path, default=ROOT / '.pa/personal')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError('端口须在 1024–65535 之间')
    if args.action == 'status':
        run_status(args)
        return
    if args.action == 'restart':
        do_stop(args)
        do_start(args)
        return
    if args.action == 'stop':
        do_stop(args)
        return
    do_start(args)


def do_stop(args):
    data = args.data.resolve()
    marker = data / 'desktop-process.json'
    saved = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
    url = f'http://127.0.0.1:{args.port}'
    current = status(url)
    if current and (not saved or current.get('instance_id') != saved.get('instance_id')):
        raise ValueError('这个端口正在运行另一份服务，请先使用它对应的停止入口，或选择其他端口。')
    if not current:
        print('服务已经停止。资料保留。')
        return
    request(url + '/api/shutdown', body={}, token=current['token'])
    for _ in range(100):
        if status(url) is None:
            print('服务已停止。下次启动继续处理已接收的工作。')
            return
        time.sleep(0.2)
    raise ValueError('停止请求已接收，但进程尚未结束，请查看数据目录中的日志。')


def do_start(args):
    data = args.data.resolve()
    marker = data / 'desktop-process.json'
    saved = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
    url = f'http://127.0.0.1:{args.port}'
    current = status(url)
    if current and (not saved or current.get('instance_id') != saved.get('instance_id')):
        raise ValueError('这个端口正在运行另一份服务，请先使用它对应的停止入口，或选择其他端口。')
    if not current:
        data.mkdir(parents=True, exist_ok=True)
        logs = data / 'logs'
        logs.mkdir(exist_ok=True)
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
        with (logs / 'server.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, '-m', 'personal_agent', '--data', str(data), 'web', '--port', str(args.port)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, start_new_session=os.name != 'nt')
        for _ in range(100):
            if proc.poll() is not None:
                raise ValueError('启动未完成，请查看数据目录 logs/server.log。常见原因是端口占用或缺少依赖。')
            current = status(url)
            if current and current.get('title') in SERVICE_TITLES:
                marker.write_text(json.dumps({'pid': proc.pid, 'port': args.port, 'data': str(data), 'instance_id': current['instance_id']}, ensure_ascii=False), encoding='utf-8')
                break
            time.sleep(0.2)
        else:
            raise ValueError('启动等待超时，请查看数据目录 logs/server.log。')
    if not args.no_browser:
        webbrowser.open(url)
    print('Noyi 已就绪：' + url)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
