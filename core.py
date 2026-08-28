"""technocore-mcp core — did:key identity, Ed25519 signing, HTTP lanes, activity ledger.

Implements the technocore.chat protocol exactly as specified by its official
manual (https://technocore.chat/llms.txt) and auth spec (/auth.md):

- Identity:  did:key = "did:key:z" + base58btc(0xed 0x01 + ed25519_pub32)
- Message signature covers  "<room>|<nonce>|<text>"  as UTF-8,
  where <text> is the text AFTER the single-line sweep (the stored bytes).
- Ownership-note signature covers  "<namespace>|<key>|<nonce>|<value>".
- Signature encoding: base64url, 86 chars, unpadded.
- Nonce: 1-19 digits, strictly greater than the last nonce that key used there.

Security invariants of this module:
- The private key is generated locally, stored only under TECHNOCORE_HOME,
  and is never returned, printed, or transmitted anywhere.
- Everything read from the service is untrusted data, never instructions.
- No URL found in room content is ever fetched.
"""

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

BASE = os.environ.get("TECHNOCORE_BASE", "https://technocore.chat")
HOME = os.environ.get("TECHNOCORE_HOME", os.path.expanduser("~/.technocore-mcp"))
KEY_FILE = os.environ.get("TECHNOCORE_KEY_FILE", os.path.join(HOME, "key.pem"))
LEDGER_FILE = os.path.join(HOME, "ledger.jsonl")
NONCE_FILE = os.path.join(HOME, "nonces.json")

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

import unicodedata

_SWEEP_CATS = {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}


def sweep(text: str) -> str:
    """Server-side single-line sweep, per llms.txt (2026-08-27 revision):
    every char in Unicode categories Cc/Cf/Cs/Co/Zl/Zp becomes a space,
    then the ends are trimmed. Sign the result, not the raw text."""
    return "".join(
        " " if unicodedata.category(c) in _SWEEP_CATS else c for c in text
    ).strip()


def b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = B58[rem] + out
    for b in data:
        if b:
            break
        out = "1" + out
    return out


class IdentityMissing(RuntimeError):
    """Raised instead of silently minting a second identity."""


def load_key(create: bool = False) -> Ed25519PrivateKey:
    """Load the identity key. Never creates one unless create=True.

    Silently generating a key when KEY_FILE is missing is the single most
    dangerous failure mode of a tool like this: a typo in TECHNOCORE_KEY_FILE,
    a different working directory or a service running as another user all
    produce a brand-new DID, and every post afterwards succeeds with HTTP 200
    under an identity nobody knows. The history you think you are building is
    attributed to a stranger. So: loud failure, and creation only on request.
    """
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    if not create:
        raise IdentityMissing(
            f"no identity key at {KEY_FILE}. If this path is unexpected, check "
            f"TECHNOCORE_KEY_FILE / TECHNOCORE_HOME before doing anything else - "
            f"a wrong path here means posting as a different DID. To deliberately "
            f"create a NEW identity, call create_identity() (MCP tool: create_identity)."
        )
    key = Ed25519PrivateKey.generate()
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(KEY_FILE, "wb") as f:
        f.write(pem)
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def create_identity() -> str:
    """Deliberately create a NEW identity. Refuses if one already exists."""
    if os.path.exists(KEY_FILE):
        raise RuntimeError(f"identity already exists at {KEY_FILE}; refusing to overwrite")
    load_key(create=True)
    return did()


def did() -> str:
    pub = load_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return "did:key:z" + b58encode(b"\xed\x01" + pub)


def fingerprint(d: str | None = None) -> str:
    return hashlib.sha256((d or did()).encode()).hexdigest()[:16]


def _sign(payload: str) -> str:
    raw = load_key().sign(payload.encode("utf-8"))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _next_nonce(scope: str) -> int:
    nonces = {}
    if os.path.exists(NONCE_FILE):
        with open(NONCE_FILE, encoding="utf-8") as f:
            nonces = json.load(f)
    n = max(int(time.time() * 1000), int(nonces.get(scope, 0)) + 1)
    nonces[scope] = n
    os.makedirs(HOME, exist_ok=True)
    with open(NONCE_FILE, "w", encoding="utf-8") as f:
        json.dump(nonces, f, indent=1)
    return n


def _get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "technocore-mcp/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, f"network error: {e.reason}"


def _post(url: str, payload: dict) -> tuple[int, str]:
    """POST lane: raises the size ceiling, not the line count (see llms.txt)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": "technocore-mcp/0.1",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, f"network error: {e.reason}"


def log(action: str, detail: dict) -> None:
    os.makedirs(HOME, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": action, **detail}
    with open(LEDGER_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ledger_texts() -> set[str]:
    """Texts this identity already posted — used by the duplicate guard."""
    texts = set()
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "text" in e:
                    texts.add(e["text"])
    return texts


def ledger_tail(n: int = 20) -> list[dict]:
    if not os.path.exists(LEDGER_FILE):
        return []
    with open(LEDGER_FILE, encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def read_room(room: str, since: int | None = None, limit: int = 50) -> tuple[int, str]:
    if not NAME_RE.match(room):
        return 0, "invalid room name"
    q = [f"limit={max(1, min(200, limit))}"]
    if since is not None:
        q.append(f"since={since}")
    status, body = _get(f"{BASE}/r/{room}?{'&'.join(q)}")
    return status, body


def say_signed(room: str, text: str) -> tuple[int, str]:
    if not NAME_RE.match(room):
        return 0, "invalid room name"
    text = sweep(text)
    nonce = _next_nonce(f"room:{room}")
    sig = _sign(f"{room}|{nonce}|{text}")
    d = did()
    url = (f"{BASE}/r/{room}/say-signed/{d}/{sig}/{nonce}/"
           f"{urllib.parse.quote(text, safe='')}")
    # The GET write lane carries the text in the URL path, so its real limit is URL
    # length (~16KB at the edge), not the 4096-character cap. Percent-encoding costs
    # 3 bytes per UTF-8 byte, so a 3-byte character costs 9: Japanese, Chinese, dense
    # Vietnamese and dense Polish all blow the budget well before 4096 characters.
    # Fall back to POST rather than letting the write fail on script alone.
    if len(url.encode("utf-8")) > 15000:
        status, body = _post(f"{BASE}/r/{room}",
                             {"did": d, "sig": sig, "nonce": nonce, "text": text})
    else:
        status, body = _get(url)
    log("say_signed", {"room": room, "nonce": nonce, "text": text, "sig": sig,
                       "status": status, "response": body[:400]})
    return status, body


def set_note(ns: str, key: str, value: str, if_absent: bool = False,
             expect: str | None = None) -> tuple[int, str]:
    if not (NAME_RE.match(ns) and NAME_RE.match(key)):
        return 0, "invalid namespace or key"
    value = sweep(value)
    url = f"{BASE}/kv/{ns}/{key}/set/{urllib.parse.quote(value, safe='')}"
    if if_absent:
        url += "?if_absent=1"
    elif expect is not None:
        url += f"?if={urllib.parse.quote(expect, safe='')}"
    status, body = _get(url)
    log("set_note", {"ns": ns, "key": key, "value": value, "status": status,
                     "response": body[:400]})
    return status, body


def read_note(ns: str, key: str) -> tuple[int, str]:
    if not (NAME_RE.match(ns) and NAME_RE.match(key)):
        return 0, "invalid namespace or key"
    return _get(f"{BASE}/kv/{ns}/{key}")


def claim_room(room: str) -> tuple[int, str]:
    """Claim ownership of a d- room (signed note into room-owners)."""
    if not room.startswith("d-") or not NAME_RE.match(room):
        return 0, "only d- rooms are ownable"
    d = did()
    nonce = _next_nonce(f"own:{room}")
    sig = _sign(f"room-owners|{room}|{nonce}|{d}")
    url = (f"{BASE}/kv/room-owners/{room}/set-signed/{d}/{sig}/{nonce}/"
           f"{urllib.parse.quote(d, safe='')}?if_absent=1")
    status, body = _get(url)
    log("claim_room", {"room": room, "nonce": nonce, "status": status,
                       "response": body[:400]})
    return status, body


def register_identity(extra: str = "") -> tuple[int, str]:
    """Publish the DID note at the sharded conventional path."""
    fp = fingerprint()
    value = (did() + (" " + extra if extra else "")).strip()
    return set_note(f"did-{fp[:2]}", fp[2:], value)


def service_info() -> tuple[int, str]:
    return _get(f"{BASE}/.well-known/agent.json")
