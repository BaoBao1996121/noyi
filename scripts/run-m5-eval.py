"""Opt-in paid Qwen evaluation. Frozen fixtures, whole-team budgets, no retries.

Run with DASHSCOPE_API_KEY in the environment. All failures remain in the output.
This compares orchestration on two synthetic document tasks, not long-term value.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from personal_agent.contracts import TaskSpec, digest, encode
from personal_agent.model import ModelConfig, QwenModel
from personal_agent.service import Service
from personal_agent.store import Store

REPO = Path(__file__).resolve().parents[1]
CASES = {
    'workshop': {
        'old': '旧版 2026-10-28：河湾工作坊负责人李青；11月8日交资料，11月10日开放报名；需要经理审批。仅供历史对照，已被新版替代。',
        'current': '现行版本 2026-11-04：河湾工作坊负责人周宁。资料整理截止2026-11-12。报名开放时间尚未确定。作者自查即可归档，不需要经理审批。需要先确认场地容量；容量未知，不得自行承诺席位。远程参加者不用到现场签到。预算额度尚未提供。',
        'notes': '会议纪要：旧版日期不可沿用。现行版本优先。新增事项没有负责人或日期时保留待确认。资料整理截止并不代表活动举行日。完成清单不能证明实际报名已开放。',
        'rubric': ['周宁负责', '资料截止11月12日而非活动日', '报名日期未知', '作者自查不增加经理审批', '场地容量/席位未定', '远程签到例外', '预算未知', '旧版显式标过时', '不声称外部动作已执行', '引用可溯源且缺失文件明确'],
    },
    'reading_club_holdout': {
        'old': '旧版：周三读书会由何夏负责，12月2日提交活动复盘，所有人必须交纸质问卷。此文件已过时。',
        'current': '新版生效2026-11-04：读书会由陈语负责。12月5日提交活动复盘。活动日期尚未确定。成员可交电子问卷，远程成员不必交问卷；不得从缺少问卷推断缺席。\n' + ('背景资料：本段只是阅读笔记索引，不是新的行动要求。\n' * 220) + '\n文末修订：负责人仍为陈语；12月5日是复盘截止。场地方案等待确认。场地联系人还未分配，不能给陈语新增场地联络责任。无需额外领导审批。',
        'notes': '从新版与文末修订出发，列出有依据的事项和未知，不添加缺失时间。问卷收集情况未提供，不能声称满意度增加。旧版只用于指出变更。',
        'rubric': ['陈语负责读书会', '复盘截止12月5日而非活动日', '活动日期未知', '电子问卷允许', '远程免问卷不推断缺席', '读到文末场地未定', '场地联系人未分配不强加陈语', '无需领导审批', '不推断满意度增长或外部已执行', '引用可溯源且缺失文件明确'],
    },
}
CORE = '根据提供的资料交付一份中文行动清单（600字以内）。明确现行负责人、日期含义、例外、待确认事项与旧版变化；保留来源引用。不增加审批/负责人/日期，不声称已执行外部动作。不确定的资料保留为未知；如可选资料缺失，仍根据可读证据交付并写出限制。完整读取所使用资料，包括分页末尾。'
MODES = {
    'solo': '组织方式：你自己完成，不委派 Worker。',
    'parallel': '组织方式：先派两个互不依赖的 Worker，两个都独立读取全部来源并各自产出完整草案。每个最多8次模型调用。等待二者完成后，逐一读取、审核，并整合你自己的最终清单；不让两者相互依赖或传话。',
    'team': '组织方式：先派两个 Worker 协作分工，每个最多8次模型调用：一位核对现行版本、负责人、时间及例外（current）；另一位核对旧版变化、纪要及缺失资料（old,notes,optional）。给每位相应的来源范围，等待完成并逐一读取审核，再交付你整合后的清单。',
}
# Counterbalanced order fixed before any result; one run/cell is exploratory.
ORDER = [('solo', False), ('team', True), ('parallel', False), ('solo', True), ('team', False), ('parallel', True)]


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k != 'location'}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


async def main(out):
    out.mkdir(parents=True, exist_ok=False)
    code = {p.relative_to(REPO).as_posix(): digest(p.read_bytes()) for p in sorted((REPO / 'src').rglob('*')) if p.is_file() and p.suffix in {'.py', '.mjs', '.js', '.css', '.html'}}
    protocol = {'cases': CASES, 'core': CORE, 'modes': MODES, 'order': ORDER, 'model': asdict(ModelConfig.load()), 'total_team_budget': {'calls': 32, 'tools': 90, 'seconds': 900, 'max_workers': 2}, 'human_interventions_per_run': 0, 'reported_human_management_seconds': None, 'invoice_currency_cost': None, 'replicates_per_cell': 1, 'holdout': 'reading_club_holdout was not used in prompt tuning', 'source_code_sha256': code, 'harness_sha256': digest(Path(__file__).read_bytes())}
    (out / 'protocol.json').write_text(encode(protocol), encoding='utf-8')
    config = ModelConfig.load()
    model = QwenModel(config)
    results = []
    try:
        for ci, (case, fixture) in enumerate(CASES.items()):
            source_dir = out / 'fixtures' / case
            source_dir.mkdir(parents=True)
            for sid in ['old', 'current', 'notes']:
                (source_dir / (sid + '.txt')).write_text(fixture[sid], encoding='utf-8')
            sources = [{'id': sid, 'kind': 'file', 'location': str((source_dir / (sid+'.txt')).resolve()), 'required': sid != 'optional'} for sid in ['old', 'current', 'notes', 'optional']]
            order = ORDER if ci == 0 else list(reversed(ORDER))
            for mode, nudge in order:
                label = f'{case}-{mode}-nudge{int(nudge)}'
                data = out / label
                store = Store(data / 'state.sqlite')
                spec = TaskSpec(CORE + '\n' + MODES[mode], sources, max_calls=32, max_tools=90, max_seconds=900, max_workers=2, max_continuations=12, enable_nudge=nudge, review_workers=True)
                tid = store.submit(spec, nonce=label)['task_id']
                started = time.monotonic()
                error = None
                try:
                    await Service(store, data, model).serve(once=True)
                except Exception as exc:
                    error = type(exc).__name__
                usage = [json.loads(r['data']) for r in store.all("SELECT data FROM steps WHERE kind='model'")]
                tasks = [store.task(t['id']) for t in store.all('SELECT id FROM tasks ORDER BY created')]
                operations = [o for task in tasks for o in store.operations(task['id'])]
                artifacts = []
                for d in store.all('SELECT * FROM deliveries WHERE artifact IS NOT NULL'):
                    meta = json.loads(d['artifact'])
                    raw = (data / 'tasks' / d['task_id'] / meta['ref']).read_bytes()
                    assert digest(raw) == meta['sha256']
                    name = d['task_id'] + '.md'
                    (data / name).write_bytes(raw)
                    artifacts.append({'task_id': d['task_id'], 'root': d['task_id'] == tid, 'file': name, 'sha256': meta['sha256']})
                result = {'label': label, 'task_id': tid, 'mode': mode, 'case': case, 'nudge_enabled': nudge, 'status': store.task(tid)['status'], 'error': error, 'seconds': round(time.monotonic()-started, 3), 'calls': len(store.all('SELECT id FROM model_requests')), 'input_tokens': sum(u['input_tokens'] for u in usage), 'output_tokens': sum(u['output_tokens'] for u in usage), 'workers': len(tasks)-1, 'nudges': len(store.all('SELECT id FROM nudges')), 'failed_tools': [{'tool': o['tool'], 'result': o['result']} for o in operations if o['status'] != 'succeeded'], 'artifacts': artifacts}
                evidence = {**result, 'tasks': public(tasks), 'operations': public(operations), 'usage': usage, 'requests': store.all('SELECT * FROM model_requests'), 'runs': store.all('SELECT * FROM runs'), 'nudges_receipts': store.all('SELECT * FROM nudges'), 'human_interventions': [], 'quality_review': 'Pending separate source comparison; structural success is not a quality grade.'}
                (data / 'evidence.json').write_text(encode(evidence), encoding='utf-8')
                results.append(result)
                (out / 'results.json').write_text(encode(results), encoding='utf-8')
                print(encode(result), flush=True)
                store.close()
    finally:
        await model.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=REPO / '.pa' / ('m5-eval-' + str(int(time.time()))))
    args = parser.parse_args()
    asyncio.run(main(args.output.resolve()))
