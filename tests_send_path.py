#!/usr/bin/env python3
"""Offline tests for the hardened wxd.py send path and wxsend.py.

No live API: the sender (wx.api_send_text) is stubbed, and all bridge
files (inbox/outbox/sent/state) are redirected to a temp directory.

Covers:
  1. a line with two concatenated JSON objects -> both recovered & sent
  2. a partial trailing line (no newline yet) -> held, sent once completed
  3. a garbage line -> recorded once in sent.jsonl as invalid, never looped
  4. send retry: transport error / API failure -> retried once, then ok
  5. errcode -14 -> SessionExpired immediately, no retry, inbox marker
  6. 10 parallel wxsend.py invocations -> 10 well-formed outbox lines
  7. wxsend to a user with NO stored context_token -> loud refusal
     (exit 2, nothing queued); with a stored token -> queues fine
     (the proactive-push path needs no fresh inbound message)

Run: python3 tests_send_path.py   (exit 0 = all passed)
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import requests  # noqa: E402  (for ConnectionError in the retry stub)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wxd = _load("wxd_under_test", "wxd.py")
wxsend = _load("wxsend_under_test", "wxsend.py")

FAILURES = []


def check(name, cond, detail=""):
    print("%s %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILURES.append(name)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class Sandbox:
    """A Bridge wired to temp files with a stubbed sender."""

    def __init__(self, tmp, sender):
        self.inbox = os.path.join(tmp, "inbox.jsonl")
        self.outbox = os.path.join(tmp, "outbox.jsonl")
        self.sent = os.path.join(tmp, "sent.jsonl")
        wxd.INBOX_PATH = self.inbox
        wxd.OUTBOX_PATH = self.outbox
        wxd.SENT_PATH = self.sent
        wxd.WXD_STATE_PATH = os.path.join(tmp, "wxd-state.json")
        wxd.wx.STATE_PATH = os.path.join(tmp, "wx-state.json")
        wxd.SEND_RETRY_DELAY = 0
        wxd.wx.api_send_text = sender
        for p in (self.inbox, self.outbox, self.sent):
            open(p, "a", encoding="utf-8").close()
        self.bridge = wxd.Bridge({"bot_token": "test-token"})

    def append_outbox_raw(self, data):
        mode = "ab" if isinstance(data, bytes) else "a"
        with open(self.outbox, mode,
                  encoding=None if mode == "ab" else "utf-8") as f:
            f.write(data)


def ok_sender(calls):
    def send(auth, user_id, text, ctx=None):
        calls.append((user_id, text))
        return {"ret": 0}, "client-%d" % len(calls)
    return send


def test_concatenated_line_recovered():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        sb = Sandbox(tmp, ok_sender(calls))
        line = json.dumps({"to_user_id": "userA", "text": "第一条"}) \
            + json.dumps({"to_user_id": "userB", "text": "第二条"}) + "\n"
        sb.append_outbox_raw(line)
        entries = sb.bridge.read_outbox_lines()
        check("concat: two entries recovered",
              len(entries) == 2
              and entries[0].get("to_user_id") == "userA"
              and entries[1].get("to_user_id") == "userB",
              repr(entries))
        # rewind and drain end-to-end through the stubbed sender
        sb.bridge.outbox_offset = 0
        sb.bridge.drain_outbox_once()
        check("concat: both messages sent",
              calls == [("userA", "第一条"), ("userB", "第二条")],
              repr(calls))
        sent = read_jsonl(sb.sent)
        check("concat: both recorded ok",
              len(sent) == 2 and all(r.get("ok") for r in sent), repr(sent))


def test_partial_line_held():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        sb = Sandbox(tmp, ok_sender(calls))
        sb.append_outbox_raw('{"to_user_id": "userA", "text": "半行')
        entries = sb.bridge.read_outbox_lines()
        check("partial: nothing consumed yet", entries == [], repr(entries))
        sb.append_outbox_raw('"}\n')
        entries = sb.bridge.read_outbox_lines()
        check("partial: completed line parsed",
              len(entries) == 1 and entries[0].get("text") == "半行",
              repr(entries))


def test_garbage_recorded_once():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        sb = Sandbox(tmp, ok_sender(calls))
        sb.append_outbox_raw("this is not json at all\n")
        sb.bridge.drain_outbox_once()
        sent = read_jsonl(sb.sent)
        invalid = [r for r in sent if r.get("error") == "invalid_outbox_line"]
        check("garbage: recorded exactly once as invalid",
              len(invalid) == 1 and calls == [], repr(sent))
        sb.bridge.drain_outbox_once()
        sent2 = read_jsonl(sb.sent)
        check("garbage: not re-recorded on next drain",
              len(sent2) == len(sent), repr(sent2))


def test_retry_transport_error():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        def flaky(auth, user_id, text, ctx=None):
            calls.append((user_id, text))
            if len(calls) == 1:
                raise requests.ConnectionError("boom")
            return {"ret": 0}, "client-ok"

        sb = Sandbox(tmp, flaky)
        ok = sb.bridge.send_one("userA", "重试我")
        sent = read_jsonl(sb.sent)
        check("retry(transport): sent on 2nd attempt",
              ok is True and len(calls) == 2, repr(calls))
        check("retry(transport): one sent record, ok, attempts=2",
              len(sent) == 1 and sent[0].get("ok") is True
              and sent[0].get("attempts") == 2, repr(sent))


def test_retry_api_failure():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        def flaky(auth, user_id, text, ctx=None):
            calls.append((user_id, text))
            if len(calls) == 1:
                return {"ret": 7, "errmsg": "busy"}, "client-x"
            return {"ret": 0}, "client-ok"

        sb = Sandbox(tmp, flaky)
        ok = sb.bridge.send_one("userA", "重试我")
        sent = read_jsonl(sb.sent)
        check("retry(api): sent on 2nd attempt",
              ok is True and len(calls) == 2, repr(calls))
        check("retry(api): one sent record, ok, attempts=2",
              len(sent) == 1 and sent[0].get("ok") is True
              and sent[0].get("attempts") == 2, repr(sent))


def test_no_retry_on_session_expired():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        def expired(auth, user_id, text, ctx=None):
            calls.append((user_id, text))
            return {"errcode": -14}, "client-x"

        sb = Sandbox(tmp, expired)
        raised = False
        try:
            sb.bridge.send_one("userA", "不要重试")
        except wxd.SessionExpired:
            raised = True
        check("expired: SessionExpired raised, exactly one attempt",
              raised and len(calls) == 1, repr(calls))
        inbox = read_jsonl(sb.inbox)
        check("expired: marker appended to inbox",
              any(r.get("event") == "session_expired" for r in inbox),
              repr(inbox))


def _seed_state(tmp, user_ids):
    state_path = os.path.join(tmp, "state.json")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"context_tokens":
                   {uid: "tok-%s" % uid for uid in user_ids}}, f)
    return state_path


def test_wxsend_concurrent():
    with tempfile.TemporaryDirectory() as tmp:
        outbox = os.path.join(tmp, "outbox.jsonl")
        state_path = _seed_state(tmp, ["user%d" % i for i in range(10)])
        env = dict(os.environ, WX_OUTBOX_PATH=outbox,
                   WX_STATE_PATH=state_path)
        script = os.path.join(HERE, "wxsend.py")
        procs = [subprocess.Popen(
            [sys.executable, script, "user%d" % i, "消息%d" % i],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for i in range(10)]
        rcs = [p.wait() for p in procs]
        check("wxsend: all 10 invocations exit 0", all(r == 0 for r in rcs),
              repr(rcs))
        with open(outbox, encoding="utf-8") as f:
            raw_lines = f.read().splitlines()
        parsed, all_valid = [], True
        for line in raw_lines:
            try:
                parsed.append(json.loads(line))
            except ValueError:
                all_valid = False
        check("wxsend: 10 lines, every one a valid single JSON object",
              len(raw_lines) == 10 and all_valid
              and {p["to_user_id"] for p in parsed}
              == {"user%d" % i for i in range(10)},
              "%d lines" % len(raw_lines))
        entries = []
        for line in raw_lines:
            entries.extend(wxd.parse_outbox_line(line))
        check("wxsend: daemon parser sees 10 valid entries, none invalid",
              len(entries) == 10
              and all("_invalid" not in e for e in entries), repr(entries))


def test_wxsend_no_token_fails_loudly():
    with tempfile.TemporaryDirectory() as tmp:
        outbox = os.path.join(tmp, "outbox.jsonl")
        state_path = _seed_state(tmp, ["someone-else"])
        env = dict(os.environ, WX_OUTBOX_PATH=outbox,
                   WX_STATE_PATH=state_path)
        script = os.path.join(HERE, "wxsend.py")
        p = subprocess.run([sys.executable, script, "never-messaged", "你好"],
                           env=env, capture_output=True, text=True)
        queued = 0
        if os.path.exists(outbox):
            with open(outbox, encoding="utf-8") as f:
                queued = len([l for l in f if l.strip()])
        check("wxsend(no token): exit 2, clear context_token error, "
              "nothing queued",
              p.returncode == 2 and "no_context_token" in p.stderr
              and queued == 0,
              "rc=%s stderr=%s queued=%d"
              % (p.returncode, p.stderr.strip()[:120], queued))
        # and the user WITH a stored token queues fine (proactive path:
        # no fresh inbound message involved, the stored token is enough)
        p2 = subprocess.run([sys.executable, script, "someone-else", "主动推送"],
                            env=env, capture_output=True, text=True)
        with open(outbox, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        check("wxsend(stored token): proactive queue succeeds",
              p2.returncode == 0 and len(lines) == 1
              and lines[0]["text"] == "主动推送",
              "rc=%s lines=%r" % (p2.returncode, lines))


def main():
    test_concatenated_line_recovered()
    test_partial_line_held()
    test_garbage_recorded_once()
    test_retry_transport_error()
    test_retry_api_failure()
    test_no_retry_on_session_expired()
    test_wxsend_concurrent()
    test_wxsend_no_token_fails_loudly()
    print("---")
    if FAILURES:
        print("FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
