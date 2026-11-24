#!/usr/bin/env python3
"""wx.py — WeChat iLink Bot API CLI client.

Connects a personal WeChat account to an AI agent via Tencent's official
iLink Bot API (see PROTOCOL.md in this directory for the wire protocol).

Subcommands:
  login             QR-code login. Saves the QR image to login-qr.png next to
                    this script, polls scan status, and on confirmation stores
                    credentials in ~/.config/wechat-bridge/auth.json (0600).
  poll [--once]     Long-poll getupdates. Prints each new message as one JSON
                    line on stdout. Persists the get_updates_buf cursor and the
                    latest context_token per user in
                    ~/.config/wechat-bridge/state.json (0600).
  send <uid> <text> Send a text message to a user. Echoes the stored
                    context_token for that user (the protocol marks it
                    REQUIRED for sendmessage; it is learned from `poll`).

Exit codes: 0 ok | 1 usage/auth/network error | 2 session expired (errcode
-14, run `login` again) | 3 QR code expired | 4 login timed out.

Credentials and state live ONLY under ~/.config/wechat-bridge/.
"""

import argparse
import base64
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
import uuid

import requests

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "1.0.0"

CONFIG_DIR = os.path.expanduser("~/.config/wechat-bridge")
AUTH_PATH = os.path.join(CONFIG_DIR, "auth.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
QR_PATH = os.path.join(BRIDGE_DIR, "login-qr.png")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def emit(obj):
    """Print one JSON object as a single line on stdout."""
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def emit_err(obj):
    """Print one JSON object as a single line on stderr."""
    print(json.dumps(obj, ensure_ascii=False), file=sys.stderr, flush=True)


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def save_json(path, data):
    """Write JSON with 0600 permissions (credentials hygiene)."""
    ensure_config_dir()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_auth():
    auth = load_json(AUTH_PATH, None)
    if not auth or not auth.get("bot_token"):
        emit_err({"event": "error", "error": "not_logged_in",
                  "detail": "no bot_token in %s; run `wx.py login` first" % AUTH_PATH})
        sys.exit(1)
    return auth


def base_url(auth):
    return ((auth or {}).get("baseurl") or DEFAULT_BASE).rstrip("/")


def base_info():
    return {"channel_version": CHANNEL_VERSION}


def post_headers(auth):
    # X-WECHAT-UIN: base64 of the decimal string of a random uint32,
    # regenerated per request (per PROTOCOL.md).
    uin = base64.b64encode(str(random.getrandbits(32)).encode("ascii")).decode("ascii")
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": "Bearer " + auth["bot_token"],
        "X-WECHAT-UIN": uin,
    }


def is_session_expired(data):
    return data.get("errcode") == -14 or data.get("ret") == -14


def response_failed(data):
    ret = data.get("ret", 0)
    err = data.get("errcode", 0)
    return (ret not in (0, None)) or (err not in (0, None))


# --------------------------------------------------------------------------
# API helpers (shared with wxd.py, which imports this file as a module)
# --------------------------------------------------------------------------

def api_getupdates(auth, cursor, timeout=40):
    """One getupdates long-poll round-trip. Returns the parsed JSON dict.
    Raises requests.RequestException / ValueError on transport errors."""
    r = requests.post(base_url(auth) + "/ilink/bot/getupdates",
                      headers=post_headers(auth),
                      json={"get_updates_buf": cursor,
                            "base_info": base_info()},
                      timeout=timeout)  # server holds ~35s
    r.raise_for_status()
    return r.json()


def api_send_text(auth, user_id, text, context_token=None):
    """One sendmessage call for a text message. Returns (data, client_id).
    Raises requests.RequestException / ValueError on transport errors."""
    msg = {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": uuid.uuid4().hex,
        "message_type": 2,   # BOT
        "message_state": 2,  # FINISH
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    r = requests.post(base_url(auth) + "/ilink/bot/sendmessage",
                      headers=post_headers(auth),
                      json={"msg": msg, "base_info": base_info()},
                      timeout=15)
    r.raise_for_status()
    return r.json(), msg["client_id"]


# --------------------------------------------------------------------------
# media: AES-128-ECB + CDN upload/download + image send/receive
# --------------------------------------------------------------------------

def _pkcs7_pad(data, block=16):
    n = block - (len(data) % block)
    return data + bytes([n]) * n


def _pkcs7_unpad(data, block=16):
    if not data or len(data) % block:
        raise ValueError("invalid padded length")
    n = data[-1]
    if n < 1 or n > block or data[-n:] != bytes([n]) * n:
        raise ValueError("invalid PKCS7 padding")
    return data[:-n]


def _aes_ecb_via_openssl(data, key, decrypt=False):
    args = ["openssl", "enc", "-aes-128-ecb", "-K", key.hex(), "-nopad"]
    if decrypt:
        args.append("-d")
    p = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError("openssl aes failed: %s" %
                           p.stderr.decode("utf-8", "replace")[:300])
    return p.stdout


def aes_encrypt_ecb(data, key):
    """AES-128-ECB encrypt with PKCS7 padding. Tries pycryptodome, then
    cryptography, then the openssl CLI (present in this sandbox)."""
    if len(key) != 16:
        raise ValueError("aes key must be 16 bytes")
    try:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_ECB).encrypt(_pkcs7_pad(data))
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(_pkcs7_pad(data)) + enc.finalize()
    except ImportError:
        pass
    return _aes_ecb_via_openssl(_pkcs7_pad(data), key)


def aes_decrypt_ecb(data, key):
    if len(key) != 16:
        raise ValueError("aes key must be 16 bytes")
    try:
        from Crypto.Cipher import AES
        return _pkcs7_unpad(AES.new(key, AES.MODE_ECB).decrypt(data))
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return _pkcs7_unpad(dec.update(data) + dec.finalize())
    except ImportError:
        pass
    return _pkcs7_unpad(_aes_ecb_via_openssl(data, key, decrypt=True))


def decode_aes_key(encoded):
    """Decode a CDNMedia aes_key to raw 16 bytes. Images use
    base64(raw 16 bytes); file/voice/video use base64(hex 32 chars);
    a bare hex string is also accepted. Raises ValueError otherwise."""
    if not encoded:
        raise ValueError("empty aes_key")
    if isinstance(encoded, bytes):
        encoded = encoded.decode("ascii")
    try:
        raw = base64.b64decode(encoded, validate=False)
    except Exception:
        raw = b""
    if len(raw) == 16:
        return raw
    for cand in (raw, encoded.encode("ascii")):
        try:
            txt = cand.decode("ascii").strip()
            if len(txt) == 32:
                b = bytes.fromhex(txt)
                if len(b) == 16:
                    return b
        except Exception:
            pass
    raise ValueError("cannot decode aes_key %r" % (encoded,))


def api_get_upload_url(auth, filekey, media_type, to_user_id, rawsize,
                       rawfilemd5, filesize, aeskey, no_need_thumb=True):
    body = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": rawsize,
        "rawfilemd5": rawfilemd5,
        "filesize": filesize,
        "aeskey": aeskey,
        "no_need_thumb": bool(no_need_thumb),
        "base_info": base_info(),
    }
    r = requests.post(base_url(auth) + "/ilink/bot/getuploadurl",
                      headers=post_headers(auth), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def cdn_upload(upload_param, filekey, encrypted_bytes):
    """POST encrypted bytes to the CDN; returns the download
    encrypt_query_param from the x-encrypted-param response header."""
    url = (CDN_BASE + "/upload?encrypted_query_param=" +
           urllib.parse.quote(upload_param, safe="") +
           "&filekey=" + urllib.parse.quote(filekey, safe=""))
    r = requests.post(url, data=encrypted_bytes,
                      headers={"Content-Type": "application/octet-stream"},
                      timeout=60)
    # A 200 with an empty body is the CDN success shape (verified live
    # 2026-11-23: status 200, Content-Length 0, x-encrypted-param set).
    # Anything else, or a 200 without the header, is a failure — never
    # let a non-200 slip through as "sent" (raise_for_status alone was
    # the only check before, and the response was never logged).
    if r.status_code != 200:
        raise RuntimeError("CDN upload: HTTP %s headers=%s body=%s" %
                           (r.status_code, dict(r.headers), r.text[:300]))
    param = r.headers.get("x-encrypted-param") or r.headers.get("X-Encrypted-Param")
    if not param:
        # some responses carry it in a JSON body instead
        try:
            param = (r.json() or {}).get("encrypt_query_param")
        except ValueError:
            param = None
    if not param:
        raise RuntimeError("CDN upload: no x-encrypted-param header; "
                           "status=%s body=%s" % (r.status_code, r.text[:300]))
    return param


def cdn_download(encrypt_query_param):
    url = (CDN_BASE + "/download?encrypted_query_param=" +
           urllib.parse.quote(encrypt_query_param, safe=""))
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def upload_media(auth, to_user_id, data, media_type=1):
    """Full upload flow for one media blob. Returns dict with
    encrypt_query_param (download param), aes_key (the OUTGOING
    CDNMedia encoding: base64 of the 32-char hex string, exactly as
    Tencent's official openclaw-weixin sender builds it —
    Buffer.from(aeskey_hex).toString('base64') in send.ts), aeskey_hex,
    filekey, rawsize, filesize.

    NOTE (2026-11-23 bug): the first version sent aes_key as
    base64(raw 16 bytes). The bot-side decoders accept both encodings,
    but the native WeChat client does not: it decoded the key wrong,
    failed to decrypt, and showed the delivered image as
    'expired or cleaned up' even though sendmessage returned ok.
    getuploadurl's `aeskey` field, by contrast, IS the bare hex string.
    """
    key = os.urandom(16)
    encrypted = aes_encrypt_ecb(data, key)
    filekey = uuid.uuid4().hex
    aeskey_hex = key.hex()
    resp = api_get_upload_url(
        auth, filekey=filekey, media_type=media_type, to_user_id=to_user_id,
        rawsize=len(data), rawfilemd5=hashlib.md5(data).hexdigest(),
        filesize=len(encrypted), aeskey=aeskey_hex, no_need_thumb=True)
    if is_session_expired(resp):
        return {"_session_expired": True, "response": resp}
    upload_param = resp.get("upload_param") or resp.get("uploadParam")
    if not upload_param:
        raise RuntimeError("getuploadurl: no upload_param in response: %s" %
                           json.dumps(resp, ensure_ascii=False)[:500])
    download_param = cdn_upload(upload_param, filekey, encrypted)
    return {
        "encrypt_query_param": download_param,
        "aes_key": base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii"),
        "aes_key_raw_b64": base64.b64encode(key).decode("ascii"),
        "aeskey_hex": aeskey_hex,
        "filekey": filekey,
        "rawsize": len(data),
        "filesize": len(encrypted),
        "response": resp,
    }


def api_send_image(auth, user_id, image_info, context_token=None):
    """Send one image message (item type 2). image_info is the dict from
    upload_media(). Returns (data, client_id)."""
    media = {
        "encrypt_query_param": image_info["encrypt_query_param"],
        "aes_key": image_info["aes_key"],
        "encrypt_type": 1,
    }
    # Official sender (openclaw-weixin send.ts) sends ONLY mid_size
    # (= ciphertext size) for images; the first version here also sent
    # hd_size with the same value, which the official sender never does.
    image_item = {
        "media": media,
        "mid_size": image_info.get("filesize") or image_info.get("rawsize"),
    }
    msg = {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": uuid.uuid4().hex,
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 2, "image_item": image_item}],
    }
    if context_token:
        msg["context_token"] = context_token
    r = requests.post(base_url(auth) + "/ilink/bot/sendmessage",
                      headers=post_headers(auth),
                      json={"msg": msg, "base_info": base_info()},
                      timeout=15)
    r.raise_for_status()
    return r.json(), msg["client_id"]


def api_send_image_file(auth, user_id, path, context_token=None):
    """Upload the image at `path` then send it. Returns
    (data, client_id, image_info). Raises on upload/transport errors;
    a getuploadurl session-expiry is surfaced as data with errcode -14."""
    with open(path, "rb") as f:
        data_bytes = f.read()
    info = upload_media(auth, user_id, data_bytes, media_type=1)
    if info.get("_session_expired"):
        return info["response"], None, info
    data, client_id = api_send_image(auth, user_id, info, context_token)
    return data, client_id, info


def download_media_item(media, aes_key_hint=None):
    """Download + decrypt one CDNMedia dict. The aes key comes from
    media.aes_key, falling back to the item-level aeskey hint."""
    param = (media or {}).get("encrypt_query_param")
    if not param:
        raise ValueError("media has no encrypt_query_param")
    key = decode_aes_key((media or {}).get("aes_key") or aes_key_hint)
    return aes_decrypt_ecb(cdn_download(param), key)


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------

def save_qr_image(img_content, path=QR_PATH):
    """Write the login QR code to `path` as a real PNG image.

    `qrcode_img_content` is a URL *string* — the QR code must encode that
    exact string. It is NOT a downloadable image: fetching it returns an
    HTML page (an earlier version of this script saved that HTML as
    login-qr.png and phones rejected it as "unsupported or corrupted").
    So the QR is generated locally: segno if available, else the `qrcode`
    library, else — last resort — the old download behaviour.
    A data: URI payload is decoded directly.
    """
    if img_content.startswith("data:"):
        _, _, b64 = img_content.partition(",")
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return path

    try:
        import segno
        segno.make(img_content, error="m").save(path, scale=10, border=3)
        return path
    except ImportError:
        pass
    try:
        import qrcode
        qrcode.make(img_content).save(path)
        return path
    except ImportError:
        pass

    emit_err({"event": "warning", "stage": "save_qr_image",
              "detail": "no local QR library (segno/qrcode); falling back "
                        "to downloading qrcode_img_content, which may not "
                        "be an image"})
    r = requests.get(img_content, timeout=15)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def is_png_file(path):
    """True if the file at `path` starts with the PNG magic bytes."""
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def cmd_login(args):
    base = DEFAULT_BASE
    try:
        r = requests.get(base + "/ilink/bot/get_bot_qrcode",
                         params={"bot_type": 3}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        emit_err({"event": "error", "stage": "get_bot_qrcode", "detail": str(e)})
        return 1

    token = data.get("qrcode")
    if not token:
        emit_err({"event": "error", "stage": "get_bot_qrcode", "response": data})
        return 1

    qr_file = None
    img = data.get("qrcode_img_content")
    if img:
        try:
            qr_file = save_qr_image(img)
            if not is_png_file(qr_file):
                emit_err({"event": "warning", "stage": "save_qr_image",
                          "detail": "saved QR file is not a PNG image"})
        except (requests.RequestException, ValueError, OSError) as e:
            emit_err({"event": "warning", "stage": "save_qr_image", "detail": str(e)})

    emit({"event": "qrcode", "qrcode": token, "qr_image": qr_file,
          "detail": "scan login-qr.png with WeChat to authorize the bot"})

    deadline = time.time() + args.timeout
    last_status = None
    while time.time() < deadline:
        try:
            sr = requests.get(base + "/ilink/bot/get_qrcode_status",
                              params={"qrcode": token},
                              headers={"iLink-App-ClientVersion": "1"},
                              timeout=15)
            sr.raise_for_status()
            st = sr.json()
        except (requests.RequestException, ValueError) as e:
            emit_err({"event": "warning", "stage": "get_qrcode_status", "detail": str(e)})
            time.sleep(2)
            continue

        status = st.get("status")
        if status != last_status:
            emit({"event": "status", "status": status})
            last_status = status

        if status == "confirmed":
            auth = {
                "bot_token": st.get("bot_token"),
                "ilink_bot_id": st.get("ilink_bot_id"),
                "ilink_user_id": st.get("ilink_user_id"),
                "baseurl": st.get("baseurl") or base,
            }
            if not auth["bot_token"]:
                emit_err({"event": "error", "stage": "confirmed_without_token",
                          "response": st})
                return 1
            save_json(AUTH_PATH, auth)
            emit({"event": "confirmed",
                  "ilink_bot_id": auth["ilink_bot_id"],
                  "ilink_user_id": auth["ilink_user_id"],
                  "auth_file": AUTH_PATH})
            return 0
        if status == "expired":
            emit({"event": "expired"})
            return 3
        time.sleep(2)

    emit({"event": "timeout", "detail": "no confirmation within %ss" % args.timeout})
    return 4


# --------------------------------------------------------------------------
# poll
# --------------------------------------------------------------------------

_ITEM_LABELS = {2: "[image]", 3: "[voice]", 4: "[file]", 5: "[video]"}


def extract_text(msg):
    parts = []
    for item in msg.get("item_list") or []:
        t = item.get("type")
        if t == 1:
            txt = (item.get("text_item") or {}).get("text")
            if txt:
                parts.append(txt)
        elif t in _ITEM_LABELS:
            parts.append(_ITEM_LABELS[t])
    return "\n".join(parts)


def extract_items(msg):
    """Structured view of a message's items, preserving media info so
    wxd.py can download/decrypt inbound images. Each entry:
    {type, text?} or {type, image_item|voice_item|file_item|video_item}
    with the raw sub-object (media/aeskey included) copied verbatim."""
    out = []
    for item in msg.get("item_list") or []:
        t = item.get("type")
        entry = {"type": t}
        if t == 1:
            entry["text"] = (item.get("text_item") or {}).get("text")
        for key in ("image_item", "voice_item", "file_item", "video_item"):
            if item.get(key):
                entry[key] = item[key]
        out.append(entry)
    return out


def cmd_poll(args):
    auth = load_auth()
    state = load_json(STATE_PATH, {}) or {}
    cursor = state.get("get_updates_buf", "")
    ctx_tokens = state.get("context_tokens", {}) or {}

    while True:
        data = None
        try:
            data = api_getupdates(auth, cursor)
        except (requests.RequestException, ValueError) as e:
            emit_err({"event": "error", "stage": "getupdates", "detail": str(e)})
            if args.once:
                return 1
            time.sleep(3)
            continue

        if is_session_expired(data):
            emit_err({"event": "session_expired", "errcode": -14,
                      "detail": "bot session expired; run `wx.py login` again"})
            return 2
        if response_failed(data):
            emit_err({"event": "error", "stage": "getupdates", "response": data})
            if args.once:
                return 1
            time.sleep(3)
            continue

        new_cursor = data.get("get_updates_buf")
        if new_cursor:
            cursor = new_cursor

        for msg in data.get("msgs") or []:
            uid = msg.get("from_user_id", "")
            if uid and msg.get("context_token"):
                ctx_tokens[uid] = msg["context_token"]
            emit({"from_user_id": uid,
                  "text": extract_text(msg),
                  "items": extract_items(msg),
                  "message_id": msg.get("message_id"),
                  "message_type": msg.get("message_type"),
                  "create_time_ms": msg.get("create_time_ms")})

        state["get_updates_buf"] = cursor
        state["context_tokens"] = ctx_tokens
        save_json(STATE_PATH, state)

        if args.once:
            return 0


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------

def cmd_send(args):
    auth = load_auth()
    state = load_json(STATE_PATH, {}) or {}
    ctx = (state.get("context_tokens") or {}).get(args.user_id)

    if not ctx:
        emit_err({"event": "warning", "stage": "sendmessage",
                  "detail": "no stored context_token for this user; PROTOCOL.md "
                            "marks context_token REQUIRED — the send may be "
                            "rejected. Run `wx.py poll` and have the user send "
                            "a message first."})

    try:
        data, client_id = api_send_text(auth, args.user_id, args.text, ctx)
    except (requests.RequestException, ValueError) as e:
        emit_err({"event": "error", "stage": "sendmessage", "detail": str(e)})
        return 1

    if is_session_expired(data):
        emit_err({"event": "session_expired", "errcode": -14,
                  "detail": "bot session expired; run `wx.py login` again"})
        return 2
    if response_failed(data):
        emit_err({"event": "error", "stage": "sendmessage", "response": data})
        return 1

    emit({"event": "sent", "to_user_id": args.user_id,
          "client_id": client_id, "response": data})
    return 0


def cmd_send_image(args):
    auth = load_auth()
    state = load_json(STATE_PATH, {}) or {}
    ctx = (state.get("context_tokens") or {}).get(args.user_id)
    try:
        data, client_id, info = api_send_image_file(auth, args.user_id,
                                                    args.path, ctx)
    except (requests.RequestException, ValueError, RuntimeError, OSError) as e:
        emit_err({"event": "error", "stage": "send_image", "detail": str(e)})
        return 1
    if is_session_expired(data):
        emit_err({"event": "session_expired", "errcode": -14,
                  "detail": "bot session expired; run `wx.py login` again"})
        return 2
    if response_failed(data):
        emit_err({"event": "error", "stage": "send_image", "response": data})
        return 1
    emit({"event": "sent_image", "to_user_id": args.user_id,
          "client_id": client_id, "path": args.path,
          "filekey": info.get("filekey"), "response": data})
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(prog="wx.py",
                                description="WeChat iLink Bot API client")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="QR-code login")
    pl.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait for scan confirmation (default 300)")
    pl.set_defaults(func=cmd_login)

    pp = sub.add_parser("poll", help="long-poll for new messages (JSON lines)")
    pp.add_argument("--once", action="store_true",
                    help="make a single getupdates call and exit")
    pp.set_defaults(func=cmd_poll)

    ps = sub.add_parser("send", help="send a text message")
    ps.add_argument("user_id", help="recipient ilink user id")
    ps.add_argument("text", help="message text")
    ps.set_defaults(func=cmd_send)

    pi = sub.add_parser("send-image", help="upload + send an image file")
    pi.add_argument("user_id", help="recipient ilink user id")
    pi.add_argument("path", help="local image file path")
    pi.set_defaults(func=cmd_send_image)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
