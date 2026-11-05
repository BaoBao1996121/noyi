import io
import json
import time
import zipfile

import pytest

from personal_agent.backup import create_backup, restore_backup
from personal_agent.contracts import TaskSpec
from personal_agent.service import Service
from personal_agent.store import Store
from conftest import ScriptedModel, tool_reply, submit_written_artifact
from test_webui import client_for


@pytest.mark.asyncio
async def test_backup_roundtrip_preserves_evidence_and_pauses_unfinished_work(store, tmp_path):
    data = tmp_path / 'data'
    (data / 'uploads').mkdir()
    source = data / 'uploads' / 'notes.txt'
    source.write_text('Saved source', encoding='utf-8')
    (data / 'credentials.key').write_text('private-test-key', encoding='utf-8')
    goal = store.create_goal(room='local', actor_id='default', title='Keep track', objective='Observe')
    spec = TaskSpec('Summarize', [{'id': 'notes', 'kind': 'file', 'location': str(source), 'required': True}])
    policy = store.create_policy(goal['id'], schedule={'kind': 'interval', 'interval_seconds': 60, 'timezone': 'Asia/Shanghai'}, template=spec)
    store.create_admission_rule(goal['id'], mode='auto', contains_text='issue', template=spec, expires=time.time()+3600)
    store.save_memory('local', key='preference', value='简洁', purpose='回复风格')
    done = store.submit(TaskSpec('Produce saved result'), nonce='completed')['task_id']
    await Service(store, data, ScriptedModel([tool_reply('write_artifact', {'name': 'result.md', 'content': 'Verified result'}), submit_written_artifact])).serve(once=True)
    pending = store.submit(spec, nonce='pending')['task_id']
    store.submit(None, text='queued chat', nonce='queued-chat')
    incoming = store.accept_conversation(spec, text='继续整理这份资料', nonce='queued-natural')
    store._notice(pending, 'local', 'pending notice')
    from personal_agent.webui import Console
    original_workspace = Console(store, data, ScriptedModel([])).workspace_id
    archive = tmp_path / 'backup.zip'
    create_backup(store, data, archive)
    with zipfile.ZipFile(archive) as z:
        assert 'credentials.key' not in z.namelist()
        assert b'private-test-key' not in b''.join(z.read(n) for n in z.namelist())
    destination = tmp_path / 'restored'
    result = restore_backup(archive, destination)
    assert result['tasks_paused'] and not result['credentials_restored']
    restored = Store(destination / 'state.sqlite')
    try:
        assert restored.task(done)['status'] == 'succeeded'
        assert restored.task(done)['calls_used'] == 2
        assert restored.task(pending)['status'] == 'paused'
        assert restored.goal(goal['id'])['status'] == 'paused'
        assert restored.one('SELECT mode FROM admission_rules')['mode'] == 'off'
        assert restored.task(pending)['spec']['sources'][0]['location'] == str(destination / 'uploads/notes.txt')
        assert restored.policy(policy['id'])['template']['sources'][0]['location'] == str(destination / 'uploads/notes.txt')
        restored_incoming = restored.one('SELECT * FROM conversation_inputs WHERE id=?', (incoming['conversation_id'],))
        assert restored_incoming['state'] == 'held_after_restore'
        assert json.loads(restored_incoming['spec'])['sources'][0]['location'] == str(destination / 'uploads/notes.txt')
        assert json.loads(restored_incoming['receipt'])['state'] == 'held_after_restore'
        retry = restored.accept_conversation(spec, text='继续整理这份资料', nonce='queued-natural')
        assert retry['state'] == 'held_after_restore' and retry['duplicate'] is True
        assert restored.claim_conversation() is None
        assert restored.memory_items('local')[0]['value'] == '简洁'
        assert restored.one("SELECT * FROM deliveries WHERE content='pending notice'")['state'] == 'held_after_restore'
        model = ScriptedModel([])
        await Service(restored, destination, model).serve(once=True)
        assert not model.requests
        delivery = restored.one('SELECT * FROM deliveries WHERE task_id=? AND artifact IS NOT NULL', (done,))
        console = Console(restored, destination, model)
        assert console.workspace_id != original_workspace
        assert console.artifact(delivery['id'])[0] == b'Verified result'
    finally:
        restored.close()
    with pytest.raises(ValueError, match='新目录'):
        restore_backup(archive, destination)
    with pytest.raises(ValueError, match='已存在'):
        create_backup(store, data, archive)


@pytest.mark.parametrize('mutation', ['traversal', 'tampered', 'duplicate'])
def test_restore_rejects_invalid_archives_without_destination(store, tmp_path, mutation):
    archive = tmp_path / 'valid.zip'
    create_backup(store, tmp_path / 'data', archive)
    bad = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(bad, 'w') as z:
        for name in original.namelist():
            raw = original.read(name)
            if mutation == 'tampered' and name == 'state.sqlite':
                raw = raw[:-1] + bytes([raw[-1] ^ 1])
            z.writestr(name, raw)
        if mutation == 'traversal':
            z.writestr('uploads/../../outside.txt', b'outside')
        if mutation == 'duplicate':
            z.writestr('STATE.SQLITE', original.read('state.sqlite'))
    with pytest.raises(ValueError):
        restore_backup(bad, tmp_path / 'destination')
    assert not (tmp_path / 'destination').exists()
    assert not (tmp_path / 'outside.txt').exists()


@pytest.mark.asyncio
async def test_web_backup_requires_token_and_returns_downloadable_archive(store, tmp_path):
    async with client_for(store, tmp_path) as client:
        denied = await client.post('/api/backup', headers={'X-PA-Token': ''})
        assert denied.status == 403
        response = await client.post('/api/backup')
        assert response.status == 200 and 'attachment;' in response.headers['Content-Disposition']
        with zipfile.ZipFile(io.BytesIO(await response.read())) as z:
            assert json.loads(z.read('manifest.json'))['restore_auto_run'] is False
