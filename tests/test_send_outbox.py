"""Sending from the page: the message shows at once, the request runs behind it,
a failed send is retried with the same nonce and the server keeps one copy."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import ScriptedModel
from personal_agent.webui import create_app

WEB = Path(__file__).resolve().parents[1] / "src" / "personal_agent" / "web"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

# A server that behaves like /api/messages: one message per nonce, the same
# receipt for a repeated nonce. `down` makes requests fail before reaching it.
HARNESS = """
import {createOutbox} from %s;
const server = {messages: [], posts: [], down: false, reject: null};
const post = async payload => {
  server.posts.push(payload.nonce);
  await new Promise(r => setTimeout(r, 5));
  if (server.down) throw new Error('Failed to fetch');
  if (server.reject) throw Object.assign(new Error(server.reject), {status: 400});
  let m = server.messages.find(x => x.nonce === payload.nonce);
  if (!m) { m = {id: 'm' + (server.messages.length + 1), nonce: payload.nonce, content: payload.text}; server.messages.push(m); }
  return {message_id: m.id, state: 'received'};
};
const saved = [];
let tick = 0;
const outbox = createOutbox({post, save: items => saved.push(JSON.parse(JSON.stringify(items))), clock: () => ++tick});
const states = room => outbox.pending(room, server.messages).map(i => i.state);
const out = {};
%s
console.log(JSON.stringify(out));
"""


def run(body):
    script = HARNESS % (json.dumps((WEB / "outbox.js").as_uri()), body)
    done = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


@needs_node
def test_a_message_shows_as_sending_at_once_and_becomes_the_real_message():
    out = run("""
      const sent = outbox.send({room: 'local', text: '你好'}, 'n1');
      out.before = outbox.pending('local', []).map(i => [i.state, i.payload.text]);  // before any network answer
      out.otherRoom = outbox.pending('group', []).length;
      await sent;
      out.afterReceipt = outbox.items.map(i => [i.state, i.messageId]);
      // The next snapshot lists the message: it is drawn from there, not twice.
      out.drawnWithSnapshot = states('local');
      outbox.reconcile('local', server.messages, 0);
      out.left = outbox.items.length;
      out.posts = server.posts;
    """)
    assert out["before"] == [["sending", "你好"]] and out["otherRoom"] == 0
    assert out["afterReceipt"] == [["sent", "m1"]]
    assert out["drawnWithSnapshot"] == [] and out["left"] == 0 and out["posts"] == ["n1"]


@needs_node
def test_a_failed_send_is_kept_and_retrying_it_never_makes_a_second_message():
    out = run("""
      server.down = true;
      await outbox.send({room: 'local', text: '在外面发的'}, 'n1').catch(e => out.error = e.message);
      out.failed = outbox.items.map(i => [i.state, i.rejected, i.payload.text]);
      out.storedFailed = saved.at(-1).map(i => i.state);
      server.down = false;
      // Two taps on retry, and the request that "failed" had in fact arrived once already.
      server.messages.push({id: 'm1', nonce: 'n1', content: '在外面发的'});
      await Promise.all([outbox.retry('n1'), outbox.retry('n1')]);
      await outbox.send({room: 'local', text: '在外面发的'}, 'n1');  // the same nonce from the composer
      out.posts = server.posts;
      out.messages = server.messages.length;
      outbox.reconcile('local', server.messages, 0);
      out.left = outbox.items.length;
    """)
    assert out["error"] == "Failed to fetch"
    assert out["failed"] == [["failed", False, "在外面发的"]] and out["storedFailed"] == ["failed"]
    assert out["posts"] == ["n1", "n1"]  # the failed try and one retry; the second tap and the resend add nothing
    assert out["messages"] == 1 and out["left"] == 0


@needs_node
def test_a_rejected_send_says_why_and_can_be_taken_back_into_the_composer():
    out = run("""
      server.reject = '资料不属于当前会话';
      await outbox.send({room: 'local', text: '看图', uploads: ['u1']}, 'n1').catch(() => {});
      out.item = outbox.items.map(i => [i.state, i.rejected, i.error]);
      const back = outbox.take('n1');
      out.back = [back.nonce, back.payload.uploads];
      out.left = outbox.items.length;
    """)
    assert out["item"] == [["failed", True, "资料不属于当前会话"]]
    assert out["back"] == ["n1", ["u1"]] and out["left"] == 0


@needs_node
def test_messages_go_out_in_order_and_a_reload_resends_what_was_cut_off_with_its_nonce():
    out = run("""
      await Promise.all([outbox.send({room: 'local', text: '一'}, 'a'), outbox.send({room: 'local', text: '二'}, 'b')]);
      out.order = server.messages.map(m => m.content);
      // The page closed while 'c' was on its way; it may or may not have arrived.
      server.messages.push({id: 'mx', nonce: 'c', content: '三'});
      outbox.load([{nonce: 'c', payload: {room: 'local', text: '三'}, state: 'sending'},
                   {nonce: 'd', payload: {room: 'local', text: '四'}, state: 'failed'},
                   {nonce: 'e', payload: {room: 'local', text: '五'}, state: 'sent'}]);
      await outbox.items[0].run;
      out.after = outbox.items.map(i => [i.nonce, i.state]);
      out.copies = server.messages.filter(m => m.nonce === 'c').length;
    """)
    assert out["order"] == ["一", "二"]
    assert out["after"] == [["c", "sent"], ["d", "failed"]]  # a sent one is in the ledger already
    assert out["copies"] == 1


@needs_node
def test_a_sent_message_this_view_never_lists_is_dropped_by_the_next_snapshot():
    out = run("""
      await outbox.send({room: 'local', text: '群里随口一句'}, 'n1');
      const sentAt = outbox.items[0].sentAt;
      outbox.reconcile('local', [], sentAt - 1);  // asked for before the receipt: keep it
      out.kept = outbox.items.length;
      outbox.reconcile('local', [], sentAt + 1);  // asked for after: the ledger has it, this view does not show it
      out.left = outbox.items.length;
    """)
    assert out["kept"] == 1 and out["left"] == 0


async def test_the_conversation_names_the_nonce_of_each_message_sent_from_the_page(store, tmp_path):
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        token = (await (await client.get("/api/bootstrap")).json())["token"]
        receipt = await (await client.post("/api/messages", json={"text": "你好", "nonce": "page-nonce-1"}, headers={"X-PA-Token": token})).json()
        messages = (await (await client.get("/api/state?room=local")).json())["messages"]
        mine = [m for m in messages if m["id"] == receipt["message_id"]]
        assert mine and mine[0]["nonce"] == "page-nonce-1"
        assert all(m["nonce"] is None for m in messages if m["source"] != "web")


def test_the_page_draws_sent_messages_from_the_outbox_and_does_not_refetch_state_after_sending():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    send = app[app.index("async function send("):app.index("function outboxLine(")]
    # The bubble is drawn before the request is awaited; no full refresh after it.
    assert send.index("$('#message-input').value=''") < send.index("outbox.send(") < send.index("await sent")
    assert "refresh(" not in send
