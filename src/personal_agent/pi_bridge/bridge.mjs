import { Agent } from '@earendil-works/pi-agent-core';
import { AssistantMessageEventStream, getSystemMessageText } from '@earendil-works/pi-ai';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const emptyUsage = () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0,
  totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } });

class BridgeProtocolError extends Error {}
class BridgeClosedError extends Error {}
class BridgeExecutionError extends Error {}

function textContent(content) {
  if (typeof content === 'string') return content;
  if (content === null || content === undefined) return '';
  if (!Array.isArray(content) || content.some(part => part.type !== 'text' || typeof part.text !== 'string')) {
    throw new BridgeProtocolError('M0 requires text content');
  }
  return content.map(part => part.text).join('');
}

function asAssistant(reply, model) {
  const original = reply.message;
  if (!original || original.role !== 'assistant') throw new BridgeProtocolError('invalid model response');
  const calls = original.tool_calls ?? [];
  if (!Array.isArray(calls)) throw new BridgeProtocolError('invalid calls');
  const content = [];
  if (textContent(original.content)) content.push({ type: 'text', text: textContent(original.content) });
  const ids = new Set();
  for (const call of calls) {
    if (typeof call.id !== 'string' || !call.id || ids.has(call.id) || !call.function || typeof call.function.name !== 'string') {
      throw new BridgeProtocolError('invalid tool identity');
    }
    ids.add(call.id);
    let args = call.function.arguments;
    if (typeof args === 'string') {
      try { args = JSON.parse(args); } catch { /* Pi rejects invalid arguments; preserve the raw response below. */ }
    }
    content.push({ type: 'toolCall', id: call.id, name: call.function.name, arguments: args });
  }
  const reason = reply.finish_reason ?? (calls.length ? 'tool_calls' : 'stop');
  const stopReason = ({ tool_calls: 'toolUse', length: 'length', stop: 'stop', aborted: 'aborted' })[reason] ?? 'error';
  const usage = emptyUsage();
  usage.input = reply.input_tokens ?? 0;
  usage.output = reply.output_tokens ?? 0;
  usage.totalTokens = usage.input + usage.output;
  return { role: 'assistant', content, api: 'openai-completions', provider: 'python-qwen',
    model: reply.model || model, responseId: reply.request_id ?? undefined, usage, stopReason,
    rawStopReason: reason, timestamp: Date.now(),
    // Original wire content preserves malformed/truncated arguments in the next model request.
    bridgeOpenAI: structuredClone(original) };
}

export function fromOpenAI(messages, model) {
  if (!Array.isArray(messages) || messages.length === 0) throw new BridgeProtocolError('messages required');
  const toolNames = new Map();
  return messages.map(message => {
    if (message.role === 'system' || message.role === 'user') {
      return { role: message.role, content: textContent(message.content), timestamp: Date.now() };
    }
    if (message.role === 'assistant') {
      for (const call of message.tool_calls ?? []) toolNames.set(call.id, call.function.name);
      return asAssistant({ message, model }, model);
    }
    if (message.role === 'tool') {
      return { role: 'toolResult', toolCallId: message.tool_call_id,
        toolName: toolNames.get(message.tool_call_id) ?? 'unknown',
        content: [{ type: 'text', text: textContent(message.content) }], isError: false, timestamp: Date.now() };
    }
    throw new BridgeProtocolError('unsupported message role');
  });
}

export function toOpenAI(messages) {
  return messages.flatMap(message => {
    if (message.role === 'system') {
      const content = getSystemMessageText(message);
      return content ? [{ role: 'system', content }] : [];
    }
    if (message.role === 'user') return [{ role: 'user', content: textContent(message.content) }];
    if (message.role === 'toolResult') {
      return [{ role: 'tool', tool_call_id: message.toolCallId, content: textContent(message.content) }];
    }
    if (message.role === 'assistant') {
      if (message.bridgeOpenAI) return [structuredClone(message.bridgeOpenAI)];
      const content = message.content.filter(part => part.type === 'text').map(part => part.text).join('');
      const tool_calls = message.content.filter(part => part.type === 'toolCall').map(call => ({
        id: call.id, type: 'function', function: { name: call.name, arguments: JSON.stringify(call.arguments) } }));
      return [{ role: 'assistant', content, ...(tool_calls.length ? { tool_calls } : {}) }];
    }
    throw new BridgeProtocolError('unsupported Pi message role');
  });
}

/** Pi owns the turn/tool loop. The host owns model transport, permissions, budgets and durable facts. */
export async function runSession(start, rpc, { signal } = {}) {
  if (start?.type !== 'start' || typeof start.model !== 'string' || !Array.isArray(start.tools)) {
    throw new BridgeProtocolError('invalid start');
  }
  let halted = false;
  let activeCalls = [];
  let finishReason = 'stop';
  const executed = new Set();
  const toolNames = new Set();
  const tools = start.tools.map(definition => {
    const fn = definition?.function;
    if (definition.type !== 'function' || !fn || typeof fn.name !== 'string' || !fn.parameters || toolNames.has(fn.name)) {
      throw new BridgeProtocolError('invalid tool schema');
    }
    toolNames.add(fn.name);
    return { name: fn.name, label: fn.name, description: fn.description ?? '', parameters: fn.parameters,
      executionMode: 'sequential', execute: async (id, args) => {
        executed.add(id);
        const response = await rpc('tool', { call: { id, function: { name: fn.name, arguments: JSON.stringify(args) } },
          batch_size: activeCalls.length });
        return { content: [{ type: 'text', text: JSON.stringify(response.result) }], details: response.result,
          isError: response.is_error === true, terminate: response.terminate === true };
      } };
  });
  // Transport-only metadata. Python provides the real endpoint, limits, key and usage receipt.
  const model = { id: start.model, name: start.model, api: 'openai-completions', provider: 'python-qwen',
    baseUrl: '', reasoning: false, input: ['text'], contextWindow: 0, maxTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
  const agent = new Agent({ initialState: { model, tools, messages: fromOpenAI(start.messages, start.model) },
    toolExecution: 'sequential',
    streamFn: async (_model, context, options) => {
      const reply = await rpc('model', { messages: toOpenAI(context.messages) });
      const stream = new AssistantMessageEventStream();
      if (reply?.halt === true || options?.signal?.aborted) {
        halted = true;
        const message = { role: 'assistant', content: [], api: 'openai-completions', provider: 'python-qwen',
          model: start.model, usage: emptyUsage(), stopReason: 'aborted', timestamp: Date.now() };
        stream.push({ type: 'error', reason: 'aborted', error: message });
        return stream;
      }
      const message = asAssistant(reply, start.model);
      activeCalls = reply.message.tool_calls ?? [];
      finishReason = message.rawStopReason;
      executed.clear();
      if (message.stopReason === 'error' || message.stopReason === 'aborted') {
        stream.push({ type: 'error', reason: message.stopReason, error: message });
      } else {
        stream.push({ type: 'done', reason: message.stopReason, message });
      }
      return stream;
    },
    finishTurn: async () => {
      if (halted || signal?.aborted) return { action: 'end' };
      const response = await rpc('turn_end', { finish_reason: finishReason, tool_call_count: activeCalls.length });
      return response?.action === 'end' ? { action: 'end' } : response?.action === 'continue' ? { action: 'continue' } : undefined;
    } });
  agent.subscribe(async event => {
    if (event.type === 'tool_execution_end' && !executed.has(event.toolCallId)) {
      const call = activeCalls.find(item => item.id === event.toolCallId);
      if (!call) throw new BridgeProtocolError('unknown rejected call');
      await rpc('tool_rejected', { call, error: textContent(event.result?.content),
        reason: finishReason === 'length' ? 'length' : toolNames.has(event.toolName) ? 'schema_validation' : 'unknown_tool' });
    }
  });
  const abort = () => agent.abort();
  signal?.addEventListener('abort', abort, { once: true });
  try {
    if (signal?.aborted) return;
    await agent.continue();
    if (agent.state.errorMessage && !halted && !signal?.aborted) throw new BridgeExecutionError();
  } finally {
    signal?.removeEventListener('abort', abort);
  }
}

export function main() {
  const pending = new Map();
  const controller = new AbortController();
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  let serial = 0;
  let started = false;
  let closed = false;
  let reportedFatal = false;
  const write = value => new Promise((resolve, reject) => {
    process.stdout.write(JSON.stringify(value) + '\n', error => error ? reject(error) : resolve());
  });
  const close = () => {
    if (closed) return;
    closed = true;
    controller.abort();
    for (const { reject } of pending.values()) reject(new BridgeClosedError());
    pending.clear();
    lines.close();
    process.stdin.pause();
  };
  const fatal = async error => {
    if (!reportedFatal && !closed) {
      reportedFatal = true;
      await write({ type: 'fatal', error_type: error?.constructor?.name || 'Error' }).catch(() => {});
    }
    process.exitCode = 1;
    close();
  };
  const rpc = (method, params) => new Promise((resolve, reject) => {
    if (closed) { reject(new BridgeClosedError()); return; }
    const id = String(++serial);
    pending.set(id, { resolve, reject });
    write({ id, type: 'request', method, params }).catch(error => {
      pending.delete(id);
      reject(error);
    });
  });
  lines.on('line', line => {
    try {
      const value = JSON.parse(line);
      if (!started) {
        if (value?.type !== 'start') throw new BridgeProtocolError();
        started = true;
        runSession(value, rpc, { signal: controller.signal }).then(async () => {
          if (!closed) await write({ type: 'done' });
          close();
        }).catch(fatal);
      } else {
        const key = String(value?.id);
        const request = pending.get(key);
        if (!request || !Object.hasOwn(value, 'result')) throw new BridgeProtocolError();
        pending.delete(key);
        request.resolve(value.result);
      }
    } catch (error) { void fatal(error); }
  });
  lines.on('close', close);
  process.stdin.on('error', () => close());
  process.stdout.on('error', () => close());
  process.once('SIGINT', close);
  process.once('SIGTERM', close);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
