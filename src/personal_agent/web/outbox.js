// Messages the user has sent that the conversation does not show back yet.
//
// A message shows the moment send is pressed, marked as sending; the request runs
// behind it. Every item keeps its nonce and exact payload, so a retry is the same
// request again and the server keeps one message however often it arrives.
// Items go out one at a time, in the order they were sent.
//
// sending → sent (receipt back) → dropped once the conversation has it: a snapshot
// lists the item's nonce (in any state: a send whose answer was lost still arrived),
// or a snapshot requested after the receipt came does not list it (the message is
// not shown in this view, so nothing would ever replace it).
// sending → failed: kept with its text, retried with the same nonce or taken back
// into the composer.
export function createOutbox({post, save = () => {}, changed = () => {}, now = () => Date.now() / 1000, clock = () => Date.now()}) {
  let items = [], chain = Promise.resolve();
  const stored = () => items.map(({run, ...rest}) => rest);
  const update = () => { save(stored()); changed(); };
  // Resolves with the receipt, or null when the item left the list before its turn.
  // Rejects with the request's error after marking the item failed.
  function deliver(item) {
    if (item.run) return item.run;  // already on its way: a second click is the same request
    Object.assign(item, {state: 'sending', error: '', rejected: false});
    update();
    const run = chain.then(async () => {
      if (!items.includes(item)) return null;
      try {
        const receipt = await post({...item.payload, nonce: item.nonce});
        if (!items.includes(item)) return receipt;
        Object.assign(item, {state: 'sent', messageId: receipt?.message_id || null, sentAt: clock(), run: null});
        update();
        return receipt;
      } catch (e) {
        if (!items.includes(item)) return null;
        // rejected: the server answered with an error, so sending it unchanged will not help.
        Object.assign(item, {state: 'failed', error: e.message || '', rejected: Boolean(e.status), run: null});
        update();
        throw e;
      }
    });
    item.run = run;
    chain = run.catch(() => {});
    return run;
  }
  const seen = messages => new Set((messages || []).map(m => m.nonce).filter(Boolean));
  return {
    get items() { return items; },
    /** Replace the list (page start, workspace switch). Items cut off mid-send go out
     *  again with their nonce, so the server still keeps one. */
    load(saved) {
      items = Array.isArray(saved) ? saved.filter(i => i && i.nonce && i.payload && ['sending', 'failed'].includes(i.state)) : [];
      for (const item of items) if (item.state === 'sending') deliver(item).catch(() => {});
      changed();
    },
    /** A nonce already in the list is that item again, never a second one. */
    send(payload, nonce, extra = {}) {
      const known = items.find(i => i.nonce === nonce);
      if (known) return known.state === 'sent' ? Promise.resolve({message_id: known.messageId, duplicate: true}) : deliver(known);
      items.push({nonce, payload, created: now(), ...extra});
      return deliver(items[items.length - 1]);
    },
    retry(nonce) {
      const item = items.find(i => i.nonce === nonce && i.state === 'failed');
      return item ? deliver(item) : Promise.resolve(null);
    },
    /** Take a failed item out, to put it back into the composer. */
    take(nonce) {
      const item = items.find(i => i.nonce === nonce && i.state === 'failed');
      if (item) { items = items.filter(i => i !== item); update(); }
      return item || null;
    },
    /** A snapshot of `room` has arrived; `requestedAt` is the clock() when it was asked for. */
    reconcile(room, messages, requestedAt = -Infinity) {
      const have = seen(messages);
      const keep = items.filter(i => !(i.payload.room === room && (have.has(i.nonce) || (i.state === 'sent' && i.sentAt <= requestedAt))));
      if (keep.length !== items.length) { items = keep; save(stored()); }
    },
    /** What to draw for `room`: items its conversation does not show yet. */
    pending(room, messages) {
      const have = seen(messages);
      return items.filter(i => i.payload.room === room && !have.has(i.nonce));
    },
  };
}
