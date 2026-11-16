"""Reply drafts shown while the model is still writing.

A draft is display only. It lives in memory, never in the ledger: it has no
delivery id, so it cannot be marked seen, cannot count as delivered and cannot
settle a promise. Only the reply the ledger accepts (promise check, quote check)
becomes a delivery. When the ledger refuses the reply, the draft is withdrawn;
when the accepted reply differs from what was shown, it is marked corrected.
Either way the page says so, so text the user glimpsed never silently turns
into something else.
"""
from __future__ import annotations

import time

KEEP_NOTICE = 30  # seconds a withdrawn/corrected notice stays visible


def _same(shown, final):
    norm = lambda s: "".join((s or "").split())
    return bool(norm(shown)) and norm(final).startswith(norm(shown))


class DraftBoard:
    def __init__(self):
        self._drafts = {}
        self.version = 0

    def _changed(self):
        self.version += 1

    def write(self, message_id, room, text):
        # Anything shown earlier for this message that the new text does not
        # continue (a withdrawn draft, a retry starting over) was revised; the
        # final reply then says it was corrected even if it matches this draft.
        old = self._drafts.get(message_id)
        revised = bool(old and old["room"] == room and (
            old.get("revised") or old["state"] in ("withdrawn", "corrected")
            or (old["state"] == "writing" and not _same(old["text"], text))))
        self._drafts[message_id] = {"room": room, "text": text, "state": "writing",
                                    "revised": revised, "updated": time.time()}
        self._changed()

    def withdraw(self, message_id):
        """The text shown so far was not accepted; say so instead of removing it silently."""
        draft = self._drafts.get(message_id)
        if draft and draft["state"] == "writing":
            draft.update(state="withdrawn", text="", updated=time.time())
            self._changed()

    def settle(self, message_id, final_text):
        """The accepted reply is now a real delivery and replaces the draft."""
        draft = self._drafts.get(message_id)
        if not draft or draft["state"] != "writing":
            return
        if not draft.get("revised") and _same(draft["text"], final_text):
            del self._drafts[message_id]
        else:
            draft.update(state="corrected", text="", updated=time.time())
        self._changed()

    def for_room(self, room):
        now = time.time()
        for mid in [m for m, d in self._drafts.items() if d["state"] != "writing" and now - d["updated"] > KEEP_NOTICE]:
            del self._drafts[mid]
        return {mid: {"text": d["text"], "state": d["state"]} for mid, d in self._drafts.items() if d["room"] == room}
