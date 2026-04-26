"""
Poll Gmail (IMAP) for new reply emails and POST them to /webhooks/email.

This bridges the gap between Resend sandbox (can only send to your Gmail)
and the conversation pipeline that needs reply turns to qualify leads.

Setup
-----
1. Gmail Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP
2. Google Account → Security → 2-Step Verification (enable if not on)
3. Google Account → Security → App Passwords → create one for "Mail / Windows"
4. Add to .env:
       GMAIL_ADDRESS=mikiasdagem@gmail.com
       GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   (16-char app password)
5. In terminal 1: uvicorn agent.main:app --reload
   In terminal 2: python scripts/poll_gmail_replies.py

Every POLL_INTERVAL seconds the script checks for unread Gmail messages,
strips quoted reply chains, and POSTs the new text to /webhooks/email.
The webhook server logs the turn count — after 3 qualifying replies the
Cal.com booking fires automatically.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import os
import re
import sys
import time

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
load_dotenv()

GMAIL_ADDRESS   = os.getenv("GMAIL_ADDRESS",      "mikiasdagem@gmail.com")
GMAIL_PASSWORD  = os.getenv("GMAIL_APP_PASSWORD", "")
_BASE_URL       = os.getenv("WEBHOOK_BASE_URL",   "http://localhost:8000")
WEBHOOK_URL     = _BASE_URL.rstrip("/") + "/webhooks/email"
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))

# Only forward replies from these senders (add more as needed)
_ALLOWED_SENDERS = {s.strip().lower() for s in os.getenv("REPLY_ALLOWED_SENDERS", GMAIL_ADDRESS).split(",") if s.strip()}

_AUTOMATED_RE = re.compile(
    r"(no.?reply|noreply|newsletter|notifications?|alerts?|donotreply|mailer|bounce|"
    r"automated|system|support@|info@|admin@|postmaster)",
    re.IGNORECASE,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_plain(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _strip_quoted(text: str) -> str:
    """Keep only the new reply text; drop anything after the quoted chain starts."""
    lines: list[str] = []
    for line in text.splitlines():
        # Gmail quoted-reply markers
        if line.startswith(">"):
            break
        # "On Mon, 26 Apr 2026 ... wrote:" header
        if re.match(r"On .{10,80}wrote:", line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()


# ── core loop ─────────────────────────────────────────────────────────────────

def _poll_once(mail: imaplib.IMAP4_SSL) -> int:
    mail.select("inbox")
    _, data = mail.search(None, "UNSEEN")
    uids = (data[0] or b"").split()
    forwarded = 0

    for uid in uids:
        _, msg_data = mail.fetch(uid, "(RFC822)")
        raw_bytes = msg_data[0][1]
        msg = email.message_from_bytes(raw_bytes)

        sender    = email.utils.parseaddr(msg.get("From", ""))[1].lower()
        subject   = msg.get("Subject", "")
        in_reply  = msg.get("In-Reply-To", "")
        thread_id = in_reply or msg.get("Message-ID") or ""
        body      = _strip_quoted(_extract_plain(msg))

        # Skip: not a reply thread
        if not in_reply:
            mail.store(uid, "+FLAGS", "\\Seen")
            print(f"[skip] not a reply  from={sender}  subject={subject[:50]}")
            continue

        # Skip: automated sender (newsletters, no-reply, etc.)
        if _AUTOMATED_RE.search(sender):
            mail.store(uid, "+FLAGS", "\\Seen")
            print(f"[skip] automated sender  from={sender}")
            continue

        # Skip: sender not in allowed list (when REPLY_ALLOWED_SENDERS is set)
        if _ALLOWED_SENDERS and sender not in _ALLOWED_SENDERS:
            mail.store(uid, "+FLAGS", "\\Seen")
            print(f"[skip] sender not in allowed list  from={sender}")
            continue

        if not sender or not body:
            mail.store(uid, "+FLAGS", "\\Seen")
            continue

        payload = {
            "from":      sender,
            "subject":   subject,
            "text":      body,
            "thread_id": thread_id,
        }

        try:
            resp = httpx.post(WEBHOOK_URL, json=payload, timeout=15)
            data_out = resp.json()
            turns  = data_out.get("turns", "?")
            status = data_out.get("status", "?")
            print(
                f"[→ webhook]  from={sender}  "
                f"http={resp.status_code}  turns={turns}  lead_status={status}"
            )
            forwarded += 1
        except Exception as exc:
            print(f"[ERROR] POST to {WEBHOOK_URL} failed: {exc}")

        # Mark read so we don't re-process on the next poll
        mail.store(uid, "+FLAGS", "\\Seen")

    return forwarded


def _connect() -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
    return mail


def main() -> None:
    if not GMAIL_PASSWORD:
        print("[ERROR] GMAIL_APP_PASSWORD is not set in .env")
        print()
        print("  Steps:")
        print("  1. Gmail Settings → Forwarding and POP/IMAP → Enable IMAP → Save")
        print("  2. myaccount.google.com → Security → 2-Step Verification (enable)")
        print("  3. myaccount.google.com → Security → App Passwords")
        print("     → Select app: Mail  |  Select device: Windows → Generate")
        print("  4. Add to .env:  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx")
        sys.exit(1)

    print(f"Gmail IMAP poller started")
    print(f"  Watching : {GMAIL_ADDRESS}")
    print(f"  Webhook  : {WEBHOOK_URL}")
    print(f"  Interval : {POLL_INTERVAL}s")
    print()

    mail = _connect()

    while True:
        try:
            n = _poll_once(mail)
            if n:
                print(f"  → forwarded {n} message(s)")
        except imaplib.IMAP4.abort:
            print("[WARN] IMAP connection dropped — reconnecting...")
            try:
                mail = _connect()
            except Exception as exc:
                print(f"[ERROR] Reconnect failed: {exc}")
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as exc:
            print(f"[ERROR] {exc}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
