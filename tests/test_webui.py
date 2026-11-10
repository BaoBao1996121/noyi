import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest

from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.webui import CONSOLE, LocalModel, create_app
from personal_agent.model import ModelConfig
from personal_agent.service import Service
from conftest import ScriptedModel, tool_reply, submit_written_artifact


@asynccontextmanager
async def client_for(store, tmp_path, model=None, run_service=False):
    app = create_app(store, tmp_path / "data", model or ScriptedModel([]), run_service=run_service)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/bootstrap")
        token = (await response.json())["token"]
        client.session.headers.update({"X-PA-Token": token})
        yield client


@pytest.mark.asyncio
async def test_desktop_shutdown_requires_token_and_signals_only_its_own_instance(store, tmp_path):
    event = asyncio.Event()
    app = create_app(store, tmp_path / 'data', ScriptedModel([]), run_service=False, shutdown_event=event)
    async with TestClient(TestServer(app)) as client:
        boot = await (await client.get('/api/bootstrap')).json()
        assert boot['version'] == '0.6.0' and boot['instance_id']
        denied = await client.post('/api/shutdown')
        assert denied.status == 403 and not event.is_set()
        response = await client.post('/api/shutdown', headers={'X-PA-Token': boot['token']})
        result = await response.json()
        assert result['instance_id'] == boot['instance_id']
        await asyncio.wait_for(event.wait(), timeout=1)


@pytest.mark.asyncio
async def test_unified_message_replies_without_task_and_retry_does_not_rebill(store, tmp_path):
    model = ScriptedModel([tool_reply('resolve_conversation', {'action':'reply','text':'你好，有什么事情需要我帮忙？', 'promise': ''})])
    async with client_for(store, tmp_path, model, run_service=True) as client:
        payload = {'text':'你好','nonce':'ordinary-conversation'}
        first = await (await client.post('/api/messages', json=payload)).json()
        assert first['task_id'] is None and first['conversation_id']
        async with asyncio.timeout(5):
            while store.one('SELECT state FROM conversation_inputs WHERE id=?',(first['conversation_id'],))['state'] == 'pending':
                await asyncio.sleep(.03)
        duplicate = await (await client.post('/api/messages', json=payload)).json()
        assert duplicate['duplicate'] and duplicate['state'] == 'resolved'
        assert len(model.requests) == 1 and not store.all('SELECT * FROM tasks')
        state = await (await client.get('/api/state')).json()
        assert len(state['messages']) == 1
        assert state['deliveries'][0]['actor_id'] == 'default'
        assert state['deliveries'][0]['content'] == '你好，有什么事情需要我帮忙？'
        assert (await client.post('/api/messages',json={**payload,'text':'改成另一个请求'})).status == 400


@pytest.mark.asyncio
async def test_unified_file_request_runs_real_pi_without_selecting_a_mode(store, tmp_path):
    document = tmp_path / '资料 版本.md'
    document.write_text('负责人小李，周五提交。',encoding='utf-8')
    model = ScriptedModel([
        tool_reply('resolve_conversation',{'action':'new_task'}),
        tool_reply('read_source',{'source_id':'user_source1'}),
        tool_reply('write_artifact',{'name':'result.md','content':'小李在周五提交。[user_source1]'}),
        submit_written_artifact,
    ])
    async with client_for(store, tmp_path, model, run_service=True) as client:
        first = await (await client.post('/api/messages',json={'text':f'请整理这份资料：`{document}`','nonce':'ordinary-file'})).json()
        async with asyncio.timeout(8):
            while True:
                task = store.one('SELECT id,status FROM tasks')
                if task and task['status'] == 'succeeded':
                    break
                await asyncio.sleep(.03)
        assert len(model.requests) == 4
        assert store.task(task['id'])['spec']['sources'][0]['location'] == str(document)
        state = await (await client.get('/api/state')).json()
        assert len(state['messages']) == 1 and state['messages'][0]['id'] == first['message_id']
        assert str(document) not in json.dumps(state['tasks'])
        assert any(d['artifact'] for d in state['deliveries'])


def test_workspace_id_is_persistent_but_service_instance_and_tokens_change(store, tmp_path):
    from personal_agent.webui import Console
    a = Console(store, tmp_path / 'data', ScriptedModel([]))
    b = Console(store, tmp_path / 'data', ScriptedModel([]))
    assert a.workspace_id == b.workspace_id
    assert a.token != b.token and a.instance_id != b.instance_id
    assert b.snapshot()['workspace_id'] == a.workspace_id


@pytest.mark.asyncio
async def test_web_upload_submit_nonce_retry_and_no_private_paths(store, tmp_path):
    async with client_for(store, tmp_path) as client:
        form = aiohttp.FormData()
        form.add_field("file", "发布前需独立复核。".encode(), filename="发布要求.md", content_type="text/plain")
        response = await client.post("/api/uploads", data=form)
        assert response.status == 200
        upload = await response.json()
        payload = {"nonce": "ui-nonce", "mode": "task", "text": "整理成行动清单", "uploads": [upload["id"]]}
        first = await (await client.post("/api/messages", json=payload)).json()
        second = await (await client.post("/api/messages", json=payload)).json()
        assert second["duplicate"] and first["task_id"] == second["task_id"]
        snapshot = await (await client.get("/api/state")).json()
        assert len(snapshot["tasks"]) == 1
        assert "location" not in snapshot["tasks"][0]["spec"]["sources"][0]
        assert str(tmp_path) not in json.dumps(snapshot)
        conflict = await client.post("/api/messages", json={**payload, "text": "Changed input"})
        assert conflict.status == 400


@pytest.mark.asyncio
async def test_web_cross_origin_host_and_missing_csrf_cannot_mutate(store, tmp_path):
    async with client_for(store, tmp_path) as client:
        payload = {"nonce": "forged", "text": "unwanted", "mode": "task"}
        missing = await client.post("/api/messages", json=payload, headers={"X-PA-Token": ""})
        assert missing.status == 403
        cross = await client.post("/api/messages", json=payload, headers={"Origin": "https://example.invalid"})
        assert cross.status == 403
        rebind = await client.get("/api/state", headers={"Host": "evil.invalid"})
        assert rebind.status == 403
        assert not store.all("SELECT * FROM tasks")


@pytest.mark.asyncio
async def test_web_artifact_is_verified_and_foreign_upload_denied(store, tmp_path):
    model = ScriptedModel([tool_reply("write_artifact", {"name": "result.md", "content": "Evidence"}), submit_written_artifact])
    tid = store.submit(TaskSpec("Result"), nonce="artifact")["task_id"]
    await Service(store, tmp_path / "data", model).serve(once=True)
    delivery = store.one("SELECT * FROM deliveries")
    async with client_for(store, tmp_path) as client:
        result = await (await client.get("/api/artifacts/" + delivery["id"])).json()
        assert result["text"] == "Evidence"
        task = store.task(tid)
        ref = json.loads(delivery["artifact"])["ref"]
        (tmp_path / "data" / "tasks" / tid / ref).write_text("Tampered", encoding="utf-8")
        assert (await client.get("/api/artifacts/" + delivery["id"])).status == 400
        foreign = await client.post("/api/messages", json={"text": "Use secret", "nonce": "foreign", "uploads": ["not-owned"]})
        assert foreign.status == 400


@pytest.mark.asyncio
async def test_web_question_control_and_true_completion_with_background_service(store, tmp_path):
    model = ScriptedModel([tool_reply("ask_user", {"question": "Choose a direction", "options": ["A", "B"]}),
                           tool_reply("write_artifact", {"name": "result.md", "content": "Choice B"}), submit_written_artifact])
    async with client_for(store, tmp_path, model, run_service=True) as client:
        receipt = await (await client.post("/api/messages", json={"text": "Need a choice", "nonce": "web-question", "mode": "task"})).json()
        async with asyncio.timeout(5):
            while store.task(receipt["task_id"])["status"] != "waiting_user":
                await asyncio.sleep(.05)
        question = store.one("SELECT * FROM attentions")
        before = len(model.requests)
        await asyncio.sleep(.4)
        assert len(model.requests) == before
        response = await client.post("/api/actions/answer", json={"question_id": question["id"], "text": "B", "nonce": "answer"})
        assert (await response.json())["state"] == "answered"
        async with asyncio.timeout(5):
            while store.task(receipt["task_id"])["status"] != "succeeded":
                await asyncio.sleep(.05)
        assert store.one("SELECT acceptance FROM tasks")["acceptance"] == "passed"


@pytest.mark.asyncio
async def test_local_model_setup_no_key_echo_no_unofficial_endpoint(store, tmp_path, monkeypatch):
    monkeypatch.delenv("PA_TEST_KEY", raising=False)
    model = LocalModel(tmp_path / "data", ModelConfig(api_key_env="PA_TEST_KEY"))
    async with client_for(store, tmp_path, model) as client:
        snapshot = await (await client.get("/api/state")).json()
        assert snapshot["ready"] is False
        secret = "test-secret-only-no-provider-call"
        bad = await client.post("/api/settings", json={"name": "qwen3.7-plus", "base_url": "https://evil.invalid/compatible-mode/v1", "key": secret})
        assert bad.status == 400 and not (tmp_path / "data/credentials.key").exists()
        good = await client.post("/api/settings", json={"name": "qwen3.7-plus", "base_url": ModelConfig().base_url, "key": secret})
        assert good.status == 200
        public = await (await client.get("/api/state")).text()
        assert secret not in public and '"ready": true' in public
        assert not store.all("SELECT * FROM model_requests")


@pytest.mark.asyncio
async def test_web_rejects_binary_and_oversize_inputs(store, tmp_path):
    async with client_for(store, tmp_path) as client:
        for raw in (b"\x00file", b"a" * 128001):
            form = aiohttp.FormData()
            form.add_field("file", raw, filename="data.txt")
            assert (await client.post("/api/uploads", data=form)).status == 400
        assert not store.all("SELECT * FROM uploads")
