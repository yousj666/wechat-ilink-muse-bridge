#!/usr/bin/env python3
"""wxsend.py — queue one outbound WeChat message for the wxd.py daemon.

This is the ONLY supported way to queue a message. It appends exactly one
JSON line ({"to_user_id": ..., "text": ...}) to outbox.jsonl while holding
an exclusive fcntl.flock on the file, and fsyncs before closing — so two
agents queueing at the same moment can never interleave bytes or glue two
JSON objects onto one line. (On 2026-11-23 two writers appended to
outbox.jsonl directly at the same time; the concatenated line failed to
parse and both messages were lost. wxd.py now recovers such lines as a
safety net, but the queue must be written through here.)

Usage:
  ./wxsend.py <to_user_id> <text>

The text may be passed as several arguments; they are joined with spaces.
Set WX_OUTBOX_PATH / WX_STATE_PATH to override file locations (tests).

Proactive push is a first-class use: no fresh inbound message is needed —
sending relies on the recipient's stored context_token, learned from any
earlier inbound message of theirs (wx.py PROTOCOL notes mark the token
REQUIRED for sendmessage, and there is no recency check on it). But a
person who has NEVER messaged the bot has no stored token and cannot be
cold-contacted — that is an iLink protocol limit, not a missing feature.
For such a recipient wxsend fails LOUDLY (exit 2, clear error, nothing
queued) instead of silently queueing a message that could never be sent.

Exit codes: 0 queued | 1 usage error | 2 no stored context_token for the
recipient (they must message the bot once first).
"""

import fcntl
import json
import os
import sys

OUTBOX_PATH = os.environ.get("WX_OUTBOX_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "outbox.jsonl")
STATE_PATH = os.environ.get("WX_STATE_PATH") or os.path.join(
    os.path.expanduser("~/.config/wechat-bridge"), "state.json")


def stored_context_token(to_user_id, state_path=STATE_PATH):
    """The recipient's stored context_token, or None if they have never
    sent the bot a message since login."""
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    return (state.get("context_tokens") or {}).get(to_user_id)


def _append_locked(entry, outbox_path):
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(outbox_path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return line


def queue_message(to_user_id, text, outbox_path=OUTBOX_PATH):
    """Append one well-formed JSON line to the outbox, under an exclusive
    lock, fsynced before the lock is released. Returns the line written."""
    return _append_locked({"to_user_id": to_user_id, "text": text},
                          outbox_path)


def queue_image(to_user_id, image_path, outbox_path=OUTBOX_PATH):
    """Queue one image entry {"to_user_id":..., "image": <abs path>}.
    Same locked+fsynced append as text; wxd.py uploads+sends it."""
    return _append_locked({"to_user_id": to_user_id,
                           "image": os.path.abspath(image_path)},
                          outbox_path)


def _fail_no_token(to_user_id):
    print(json.dumps(
        {"event": "error", "error": "no_context_token",
         "detail": "no stored context_token for %s in %s — the iLink "
                   "protocol requires one (learned from an inbound "
                   "message), so this message could never be sent. "
                   "NOT queued. Have that user send the bot any "
                   "message first, let the bridge receive it, then "
                   "retry." % (to_user_id, STATE_PATH)},
        ensure_ascii=False), file=sys.stderr)
    return 2


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # image form: wxsend.py --image <path> <to_user_id>
    if argv and argv[0] == "--image":
        if len(argv) != 3:
            print(json.dumps({"event": "error", "error": "usage",
                              "detail": "usage: wxsend.py --image <path> <to_user_id>"}),
                  file=sys.stderr)
            return 1
        image_path, to_user_id = argv[1], argv[2]
        if not os.path.isfile(image_path):
            print(json.dumps({"event": "error", "error": "image_not_found",
                              "detail": image_path}), file=sys.stderr)
            return 1
        if not stored_context_token(to_user_id):
            return _fail_no_token(to_user_id)
        queue_image(to_user_id, image_path)
        print(json.dumps({"event": "queued", "kind": "image",
                          "to_user_id": to_user_id,
                          "image": os.path.abspath(image_path)},
                         ensure_ascii=False))
        return 0
    if len(argv) < 2:
        print(json.dumps({"event": "error", "error": "usage",
                          "detail": "usage: wxsend.py <to_user_id> <text> | wxsend.py --image <path> <to_user_id>"}),
              file=sys.stderr)
        return 1
    to_user_id, text = argv[0], " ".join(argv[1:])
    if not stored_context_token(to_user_id):
        print(json.dumps(
            {"event": "error", "error": "no_context_token",
             "detail": "no stored context_token for %s in %s — the iLink "
                       "protocol requires one (learned from an inbound "
                       "message), so this message could never be sent. "
                       "NOT queued. Have that user send the bot any "
                       "message first, let the bridge receive it, then "
                       "retry." % (to_user_id, STATE_PATH)},
            ensure_ascii=False), file=sys.stderr)
        return 2
    queue_message(to_user_id, text)
    print(json.dumps({"event": "queued", "to_user_id": to_user_id},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
