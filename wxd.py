#!/usr/bin/env python3
"""wxd.py — daemon-style WeChat iLink bridge runner.

Wraps the client in wx.py (imported as a module) into a file-based bridge
(JSON lines, credentials in ~/.config/wechat-bridge/, state files
alongside the scripts):

  inbox.jsonl   (this dir) one JSON line per inbound message:
                {ts, from_user_id, text, message_id, create_time_ms}
                Non-text items appear as [image]/[voice]/[file]/[video]
                placeholders in `text` (media download is future work).
  outbox.jsonl  (this dir) queue lines of {to_user_id, text} to send —
                ALWAYS via wxsend.py (locked append); never append by hand.
                Consecutive lines to the same user arriving within
                MERGE_WINDOW seconds are merged into ONE message,
                newline-joined (连发消息合并). Sent lines are consumed
                exactly once; the read offset persists across restarts in
                ~/.config/wechat-bridge/wxd-state.json. A line holding
                several concatenated JSON objects (two writers raced) is
                recovered object-by-object instead of being dropped, and
                a failed sendmessage is retried once before it is recorded
                as failed in sent.jsonl.
  sent.jsonl    (this dir) one JSON line per send outcome:
                {ts, to_user_id, text, ok, client_id | error, merged,
                attempts}

The poller long-polls getupdates continuously (server holds ~35s), so
inbound messages land in inbox.jsonl within seconds. The outbox watcher
runs in its own thread, so proactive pushes are not stuck behind the
long poll.

On errcode -14 (session expired) a {"event": "session_expired", ...}
marker line is appended to inbox.jsonl and the daemon exits with code 2 —
there is no refresh flow; the user must run `wx.py login` again.

Usage:
  ./wxd.py           run the daemon (Ctrl-C to stop)
  ./wxd.py --once    single poll cycle + single outbox drain, then exit

Exit codes: 0 ok | 1 usage/auth/network error | 2 session expired.
"""

import argparse
import importlib.util
import json
import os
import sys
import threading
import time

import requests

# ---------------------------------------------------------------------------
# import wx.py (same directory) as a module — it holds the wire client
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "wx_client", os.path.join(_HERE, "wx.py"))
wx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wx)

INBOX_PATH = os.path.join(_HERE, "inbox.jsonl")
OUTBOX_PATH = os.path.join(_HERE, "outbox.jsonl")
SENT_PATH = os.path.join(_HERE, "sent.jsonl")
MEDIA_DIR = os.path.join(_HERE, "media")
WXD_STATE_PATH = os.path.join(wx.CONFIG_DIR, "wxd-state.json")

MERGE_WINDOW = 2.0      # seconds: merge same-user outbox lines within this
WATCH_INTERVAL = 0.25   # seconds between outbox file checks / merge flushes
POLL_BACKOFF = 3        # seconds to wait after a failed getupdates call
SEND_RETRY_DELAY = 2.0  # seconds before the single sendmessage retry


def log(obj):
    obj = dict(obj)
    obj.setdefault("ts", int(time.time() * 1000))
    print(json.dumps(obj, ensure_ascii=False), file=sys.stderr, flush=True)


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()


class SessionExpired(Exception):
    pass


def _valid_outbox_obj(obj):
    return isinstance(obj, dict) and bool(obj.get("to_user_id")) \
        and (obj.get("text") is not None or bool(obj.get("image")))


def parse_outbox_line(raw):
    """Parse one outbox line into a list of entries.

    The normal case is a single JSON object per line. But two writers
    appending at the same moment can glue two (or more) JSON objects onto
    one physical line — that exact accident lost two real messages on
    2026-11-23 when the line was written off as invalid. So on a plain
    json.loads failure, walk the line with JSONDecoder.raw_decode and
    recover every complete object in it. Valid objects come back as
    entries; anything else (a field-less object, unparseable residue)
    comes back as {"_invalid": "<raw fragment>"} so the caller records
    it in sent.jsonl exactly once instead of dropping it silently or
    retrying it forever."""
    try:
        objs = [json.loads(raw)]
    except ValueError:
        objs = []
        decoder = json.JSONDecoder()
        idx, end = 0, len(raw)
        residue = None
        while True:
            while idx < end and raw[idx].isspace():
                idx += 1
            if idx >= end:
                break
            try:
                obj, idx = decoder.raw_decode(raw, idx)
            except ValueError:
                residue = raw[idx:].strip()
                break
            objs.append(obj)
        entries = [obj if _valid_outbox_obj(obj) else
                   {"_invalid": obj if isinstance(obj, str) else
                    json.dumps(obj, ensure_ascii=False)} for obj in objs]
        if residue:
            entries.append({"_invalid": residue})
        return entries or [{"_invalid": raw}]
    return [objs[0]] if _valid_outbox_obj(objs[0]) else [{"_invalid": raw}]


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, auth):
        self.auth = auth
        state = wx.load_json(wx.STATE_PATH, {}) or {}
        self.cursor = state.get("get_updates_buf", "")
        self.ctx_tokens = state.get("context_tokens", {}) or {}
        self.state_lock = threading.Lock()
        self.inbox_lock = threading.Lock()
        self.sent_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.expired_event = threading.Event()
        self._expired_marked = False
        wstate = wx.load_json(WXD_STATE_PATH, {}) or {}
        self.outbox_offset = int(wstate.get("outbox_offset", 0))

    # -- shared state --------------------------------------------------------

    def save_wx_state(self):
        with self.state_lock:
            wx.save_json(wx.STATE_PATH, {
                "get_updates_buf": self.cursor,
                "context_tokens": self.ctx_tokens,
            })

    def context_token_for(self, user_id):
        with self.state_lock:
            return self.ctx_tokens.get(user_id)

    def mark_session_expired(self, stage):
        with self.inbox_lock:
            if not self._expired_marked:
                self._expired_marked = True
                append_jsonl(INBOX_PATH, {
                    "ts": int(time.time() * 1000),
                    "event": "session_expired",
                    "stage": stage,
                    "detail": "bot session expired (errcode -14); "
                              "run `wx.py login` again",
                })
        self.expired_event.set()
        self.stop_event.set()

    # -- receiving -----------------------------------------------------------

    def poll_cycle(self):
        """One getupdates round-trip; appends new messages to inbox.jsonl.
        Returns the number of messages received.
        Raises SessionExpired on errcode -14."""
        data = wx.api_getupdates(self.auth, self.cursor)
        if wx.is_session_expired(data):
            self.mark_session_expired("getupdates")
            raise SessionExpired()
        if wx.response_failed(data):
            raise RuntimeError("getupdates failed: %s" %
                               json.dumps(data, ensure_ascii=False))
        new_cursor = data.get("get_updates_buf")
        if new_cursor:
            self.cursor = new_cursor
        count = 0
        for msg in data.get("msgs") or []:
            uid = msg.get("from_user_id", "")
            if uid and msg.get("context_token"):
                with self.state_lock:
                    self.ctx_tokens[uid] = msg["context_token"]
            record = {
                "ts": int(time.time() * 1000),
                "from_user_id": uid,
                "text": wx.extract_text(msg),
                "items": wx.extract_items(msg),
                "message_id": msg.get("message_id"),
                "create_time_ms": msg.get("create_time_ms"),
            }
            media = self._save_inbound_media(msg, record["ts"])
            if media:
                record["media"] = media
            with self.inbox_lock:
                append_jsonl(INBOX_PATH, record)
            count += 1
        self.save_wx_state()
        return count

    def _save_inbound_media(self, msg, ts):
        """Download+decrypt inbound image items to MEDIA_DIR. Returns a
        list of {kind, local_path} / {kind, error} entries (empty if the
        message has no image items). Failures are recorded, not raised:
        a bad image must not kill the poll loop."""
        saved = []
        for item in wx.extract_items(msg):
            if item.get("type") != 2:
                continue
            img = item.get("image_item") or {}
            try:
                raw = wx.download_media_item(img.get("media"),
                                             aes_key_hint=img.get("aeskey"))
                os.makedirs(MEDIA_DIR, exist_ok=True)
                ext = ".jpg"
                if raw[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                elif raw[:3] == b"\xff\xd8\xff":
                    ext = ".jpg"
                elif raw[:4] == b"GIF8":
                    ext = ".gif"
                path = os.path.join(
                    MEDIA_DIR, "in-%s-%s%s" % (ts, msg.get("message_id"), ext))
                with open(path, "wb") as f:
                    f.write(raw)
                saved.append({"kind": "image", "local_path": path,
                              "bytes": len(raw)})
            except Exception as e:
                saved.append({"kind": "image",
                              "error": "%s: %s" % (type(e).__name__, e)})
                log({"event": "inbound_media_error", "detail": str(e)})
        return saved

    def poll_loop(self):
        while not self.stop_event.is_set():
            try:
                self.poll_cycle()
            except SessionExpired:
                return
            except (requests.RequestException, ValueError, RuntimeError) as e:
                log({"event": "poll_error", "detail": str(e)})
                self.stop_event.wait(POLL_BACKOFF)

    # -- sending -------------------------------------------------------------

    def send_one(self, user_id, text, merged=1):
        """Send one (possibly merged) text message; record the outcome in
        sent.jsonl (exactly one record per message, with an "attempts"
        count). A transient failure — transport error, or an API failure
        that is not errcode -14 — is retried ONCE after SEND_RETRY_DELAY
        seconds: a queued message must not be lost to a momentary blip.
        Raises SessionExpired on errcode -14 (never retried)."""
        ctx = self.context_token_for(user_id)
        record = {"ts": int(time.time() * 1000), "to_user_id": user_id,
                  "text": text, "merged": merged}
        last_error = None
        for attempt in (1, 2):
            try:
                data, client_id = wx.api_send_text(self.auth, user_id,
                                                   text, ctx)
            except (requests.RequestException, ValueError) as e:
                last_error = str(e)
            else:
                if wx.is_session_expired(data):
                    record.update({"ok": False, "error": "session_expired",
                                   "attempts": attempt})
                    with self.sent_lock:
                        append_jsonl(SENT_PATH, record)
                    self.mark_session_expired("sendmessage")
                    raise SessionExpired()
                if not wx.response_failed(data):
                    record.update({"ok": True, "client_id": client_id,
                                   "attempts": attempt})
                    with self.sent_lock:
                        append_jsonl(SENT_PATH, record)
                    return True
                last_error = "api: " + json.dumps(data, ensure_ascii=False)
            if attempt == 1:
                log({"event": "send_retry", "to_user_id": user_id,
                     "detail": last_error})
                time.sleep(SEND_RETRY_DELAY)
        record.update({"ok": False, "error": last_error, "attempts": 2})
        with self.sent_lock:
            append_jsonl(SENT_PATH, record)
        log({"event": "send_error", "to_user_id": user_id,
             "detail": last_error})
        return False

    def send_image_one(self, user_id, image_path, merged=1):
        """Upload + send one image file; record the outcome in sent.jsonl
        as {kind: "image", image: <path>, ok, ...}. Same retry-once /
        never-retry-on--14 policy as send_one."""
        ctx = self.context_token_for(user_id)
        record = {"ts": int(time.time() * 1000), "to_user_id": user_id,
                  "kind": "image", "image": image_path, "merged": merged}
        last_error = None
        for attempt in (1, 2):
            try:
                data, client_id, info = wx.api_send_image_file(
                    self.auth, user_id, image_path, ctx)
            except (requests.RequestException, ValueError, RuntimeError,
                    OSError) as e:
                last_error = str(e)
            else:
                if wx.is_session_expired(data):
                    record.update({"ok": False, "error": "session_expired",
                                   "attempts": attempt})
                    with self.sent_lock:
                        append_jsonl(SENT_PATH, record)
                    self.mark_session_expired("sendmessage_image")
                    raise SessionExpired()
                if not wx.response_failed(data):
                    record.update({"ok": True, "client_id": client_id,
                                   "filekey": info.get("filekey"),
                                   "attempts": attempt})
                    with self.sent_lock:
                        append_jsonl(SENT_PATH, record)
                    return True
                last_error = "api: " + json.dumps(data, ensure_ascii=False)
            if attempt == 1:
                log({"event": "send_image_retry", "to_user_id": user_id,
                     "detail": last_error})
                time.sleep(SEND_RETRY_DELAY)
        record.update({"ok": False, "error": last_error, "attempts": 2})
        with self.sent_lock:
            append_jsonl(SENT_PATH, record)
        log({"event": "send_image_error", "to_user_id": user_id,
             "detail": last_error})
        return False

    def _flush_entries(self, user_id, entries):
        """Send a user's queued entries in order: runs of consecutive
        text entries are merged into one text send (existing behaviour);
        each image entry is uploaded+sent individually."""
        texts = []

        def flush_texts():
            if texts:
                self.send_one(user_id, "\n".join(texts), merged=len(texts))
                texts.clear()

        for entry in entries:
            if entry.get("image"):
                flush_texts()
                self.send_image_one(user_id, str(entry["image"]))
            else:
                texts.append(str(entry["text"]))
        flush_texts()

    # -- outbox --------------------------------------------------------------

    def _save_outbox_offset(self):
        wx.save_json(WXD_STATE_PATH, {"outbox_offset": self.outbox_offset})

    def read_outbox_lines(self):
        """Return newly appended, complete (newline-terminated) outbox
        entries since the last read. Each entry is a dict; a line holding
        several concatenated JSON objects is recovered into one entry per
        object (see parse_outbox_line), and only truly unparseable
        residue comes back as {"_invalid": "<raw fragment>"} so the
        caller can log-and-consume it instead of retrying forever."""
        try:
            size = os.path.getsize(OUTBOX_PATH)
        except OSError:
            return []
        if size < self.outbox_offset:  # truncated / rotated: start over
            self.outbox_offset = 0
        if size == self.outbox_offset:
            return []
        entries = []
        with open(OUTBOX_PATH, "rb") as f:
            f.seek(self.outbox_offset)
            chunk = f.read()
        # only consume up to and including the last newline — a trailing
        # partial line may still be being written
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return []
        consumed = chunk[:last_nl + 1]
        self.outbox_offset += len(consumed)
        for raw in consumed.decode("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            entries.extend(parse_outbox_line(raw))
        if entries:
            self._save_outbox_offset()
        return entries

    def _dispatch_entry(self, entry, pending):
        """Route one outbox entry: invalid lines are recorded in sent.jsonl
        and dropped; valid ones join the per-user merge buffer."""
        if "_invalid" in entry:
            with self.sent_lock:
                append_jsonl(SENT_PATH, {
                    "ts": int(time.time() * 1000), "ok": False,
                    "error": "invalid_outbox_line", "raw": entry["_invalid"],
                    "merged": 0,
                })
            log({"event": "outbox_invalid_line", "raw": entry["_invalid"]})
            return
        buf = pending.setdefault(entry["to_user_id"],
                                 {"entries": [], "last": 0.0})
        buf["entries"].append(entry)
        buf["last"] = time.time()

    def outbox_loop(self):
        """Daemon mode: collect outbox lines into per-user buffers and
        flush a user's buffer once its newest line is MERGE_WINDOW old,
        joining the texts with newlines into a single sendmessage call."""
        pending = {}
        while not self.stop_event.is_set():
            try:
                for entry in self.read_outbox_lines():
                    self._dispatch_entry(entry, pending)
                now = time.time()
                for uid in list(pending):
                    buf = pending[uid]
                    if now - buf["last"] >= MERGE_WINDOW:
                        del pending[uid]
                        self._flush_entries(uid, buf["entries"])
            except SessionExpired:
                return
            except Exception as e:  # keep the watcher alive on surprises
                log({"event": "outbox_error", "detail": str(e)})
            self.stop_event.wait(WATCH_INTERVAL)
        # shutdown: flush whatever is still buffered
        for uid, buf in pending.items():
            try:
                self._flush_entries(uid, buf["entries"])
            except SessionExpired:
                return

    def drain_outbox_once(self):
        """--once mode: send everything currently in the outbox, merging
        runs of consecutive text lines to the same user, without waiting.
        Image entries keep their order and are sent individually."""
        groups = []  # list of [user_id, [entries]]
        for entry in self.read_outbox_lines():
            if "_invalid" in entry:
                self._dispatch_entry(entry, {})
                continue
            uid = entry["to_user_id"]
            if groups and groups[-1][0] == uid:
                groups[-1][1].append(entry)
            else:
                groups.append([uid, [entry]])
        for uid, entries in groups:
            self._flush_entries(uid, entries)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="wxd.py",
        description="Daemon-style WeChat iLink bridge: long-polls messages "
                    "into inbox.jsonl and sends lines appended to "
                    "outbox.jsonl (same-user lines within %ss are merged)."
                    % MERGE_WINDOW)
    p.add_argument("--once", action="store_true",
                   help="single poll cycle + single outbox drain, then exit")
    args = p.parse_args(argv)

    auth = wx.load_auth()  # exits 1 with a clear message if not logged in
    bridge = Bridge(auth)

    # make sure the queue files exist so appenders never race a missing file
    for path in (INBOX_PATH, OUTBOX_PATH, SENT_PATH):
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()

    if args.once:
        try:
            n = bridge.poll_cycle()
            log({"event": "poll_once", "messages": n})
            bridge.drain_outbox_once()
        except SessionExpired:
            return 2
        except (requests.RequestException, ValueError, RuntimeError) as e:
            log({"event": "error", "detail": str(e)})
            return 1
        return 2 if bridge.expired_event.is_set() else 0

    log({"event": "started", "inbox": INBOX_PATH, "outbox": OUTBOX_PATH,
         "sent": SENT_PATH})
    threads = [
        threading.Thread(target=bridge.poll_loop, name="poll", daemon=True),
        threading.Thread(target=bridge.outbox_loop, name="outbox",
                         daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        while not bridge.stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        bridge.stop_event.set()
    for t in threads:
        t.join(timeout=5)
    if bridge.expired_event.is_set():
        log({"event": "session_expired",
             "detail": "run `wx.py login` again"})
        return 2
    log({"event": "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
