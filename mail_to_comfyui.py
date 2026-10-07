#!/usr/bin/env python3
"""
mail_to_comfyui.py

Watches an IMAP mailbox via IDLE. When a new email arrives from an allowed
sender, it parses a prompt (and optional negative prompt) from the body,
submits it to a running ComfyUI instance using a pre-exported API workflow,
waits for the render to finish, and replies to the original email with the
generated image (or audio, for TTS workflows) attached.

Requires:
    pip install imapclient python-dotenv requests

Config is read from a .env file in the same directory (see .env.example).
Set ENV_FILE to use a different file, so several mailboxes can each run
their own instance with their own workflow (e.g. ENV_FILE=tts.env).
"""

import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import time
import traceback
import unicodedata
import uuid
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import requests
from dotenv import load_dotenv
from imapclient import IMAPClient

# Load only the named file: falling back to the default .env would leak the
# image bot's settings into any other instance for keys it doesn't set.
load_dotenv(os.environ.get("ENV_FILE", ".env"))

# Older Pythons don't know these, which would send the audio as octet-stream.
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/ogg", ".opus")

# ---- Config -----------------------------------------------------------

IMAP_HOST = os.environ["IMAP_HOST"]
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
EMAIL_USER = os.environ["EMAIL_USER"]
EMAIL_PASS = os.environ["EMAIL_PASS"]

# Comma-separated list of sender addresses allowed to trigger a render.
# Leave empty to allow anyone (NOT recommended for a public mailbox).
ALLOWED_SENDERS = {
    s.strip().lower()
    for s in os.environ.get("ALLOWED_SENDERS", "").split(",")
    if s.strip()
}

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
WORKFLOW_PATH = os.environ["WORKFLOW_PATH"]  # path to exported API-format JSON
POSITIVE_NODE_ID = os.environ["POSITIVE_NODE_ID"]
NEGATIVE_NODE_ID = os.environ.get("NEGATIVE_NODE_ID")  # optional
SAVE_IMAGE_NODE_ID = os.environ["SAVE_IMAGE_NODE_ID"]  # the Save Image / Save Audio node

# "image" for image workflows, "audio" for TTS workflows.
OUTPUT_KIND = os.environ.get("OUTPUT_KIND", "image").strip().lower()
if OUTPUT_KIND not in ("image", "audio"):
    sys.exit(f"OUTPUT_KIND must be 'image' or 'audio', got {OUTPUT_KIND!r}")
# Key under which ComfyUI reports the save node's files in /history.
OUTPUT_HISTORY_KEY = {"image": "images", "audio": "audio"}[OUTPUT_KIND]

# Name of the text input on the POSITIVE_NODE_ID node. CLIPTextEncode uses
# "text"; some TTS nodes call it something else (check the exported JSON).
PROMPT_INPUT_NAME = os.environ.get("PROMPT_INPUT_NAME", "text")

# Optional: a Load Audio node whose file is picked by the email subject, so
# the subject selects the TTS voice (e.g. "anna" -> anna.wav in ComfyUI/input).
VOICE_NODE_ID = os.environ.get("VOICE_NODE_ID")
VOICE_INPUT_NAME = os.environ.get("VOICE_INPUT_NAME", "audio")

RENDER_TIMEOUT_SECONDS = int(os.environ.get("RENDER_TIMEOUT_SECONDS", "300"))
IDLE_TIMEOUT_SECONDS = 29 * 60  # RFC 2177 recommends re-issuing IDLE before 30 min

DEFAULT_NEGATIVE = os.environ.get(
    "DEFAULT_NEGATIVE_PROMPT",
    "blurry, distorted, extra limbs, bad anatomy, low quality",
)

PROCESSED_FOLDER = os.environ.get("PROCESSED_FOLDER", "Processed")


# ---- Prompt parsing -----------------------------------------------------

PROMPT_RE = re.compile(r"^\s*Prompt\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
NEGATIVE_RE = re.compile(r"^\s*Negative\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


# English and Swedish mail clients ("On ... wrote:" / "Den ... skrev:").
QUOTE_HEADER_RE = re.compile(r"^\s*(On .+ wrote|Den .+ skrev .+):\s*$", re.MULTILINE)
SIGNATURE_SEP_RE = re.compile(r"^\s*--\s*$", re.MULTILINE)


def clean_body(body_text):
    """
    Strip common quoted-reply chains and signature blocks before treating
    the body as a fallback prompt, so replying to a thread or having an
    email signature doesn't pollute the prompt text.
    """
    for pattern in (QUOTE_HEADER_RE, SIGNATURE_SEP_RE):
        match = pattern.search(body_text)
        if match:
            body_text = body_text[: match.start()]

    # Drop lines that are quoted text (start with '>')
    lines = [ln for ln in body_text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def extract_prompts(body_text):
    """Pull 'Prompt: ...' and 'Negative: ...' lines out of the email body.

    If there's no explicit 'Prompt:' line, fall back to treating the
    cleaned-up body (quoted replies and signature stripped) as the prompt.

    For audio (TTS) the whole cleaned body is the text to speak, since it
    is usually several lines/paragraphs, and there is no negative prompt.
    """
    if OUTPUT_KIND == "audio":
        return clean_body(body_text) or None, None

    pos_match = PROMPT_RE.search(body_text)
    neg_match = NEGATIVE_RE.search(body_text)

    if pos_match:
        positive = pos_match.group(1).strip()
    else:
        positive = clean_body(body_text) or None

    negative = neg_match.group(1).strip() if neg_match else DEFAULT_NEGATIVE
    return positive, negative


def get_plain_text_body(msg):
    """Extract the plain-text part of a parsed email.message.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")


# ---- ComfyUI interaction ------------------------------------------------

def load_workflow_template():
    with open(WORKFLOW_PATH, "r") as f:
        return json.load(f)


REPLY_PREFIX_RE = re.compile(r"^\s*((re|sv|fw|fwd|vb)\s*:\s*)+", re.IGNORECASE)


def available_voices():
    """
    Ask ComfyUI which files the voice node can load, keyed by lowercase
    name without extension. Asked per email, so a sample dropped into
    ComfyUI/input is usable right away without a restart.
    """
    class_type = load_workflow_template()[VOICE_NODE_ID]["class_type"]
    resp = requests.get(f"{COMFYUI_URL}/object_info/{class_type}", timeout=30)
    resp.raise_for_status()
    spec = resp.json()[class_type]["input"]["required"][VOICE_INPUT_NAME]
    # Newer ComfyUI: ["COMBO", {"options": [...]}]; older: [[...], {...}]
    files = spec[1].get("options", []) if spec[0] == "COMBO" else spec[0]
    return {voice_key(os.path.splitext(os.path.basename(f))[0]): f for f in files}


def voice_key(name):
    # NFC so "Å" typed in a subject matches a file name saved decomposed (e.g. from a Mac).
    return unicodedata.normalize("NFC", name).strip().lower()


def pick_voice(subject):
    """Map the email subject to a voice file. Returns (file or None, note for the reply)."""
    wanted = REPLY_PREFIX_RE.sub("", subject).strip()
    voices = available_voices()
    default = load_workflow_template()[VOICE_NODE_ID]["inputs"][VOICE_INPUT_NAME]
    default_name = os.path.splitext(os.path.basename(default))[0]
    names = ", ".join(sorted(voices)) or "(none)"

    if voice_key(wanted) in voices:
        return voices[voice_key(wanted)], f"Voice: {voice_key(wanted)}"
    if not wanted:
        return None, (f"Voice: {default_name} (default). Put a voice name in the "
                      f"subject to pick another. Available: {names}")
    return None, (f"There's no voice called {wanted!r}, so the default "
                  f"({default_name}) was used. Available: {names}")


def submit_render(positive_prompt, negative_prompt, voice=None):
    workflow = load_workflow_template()

    workflow[POSITIVE_NODE_ID]["inputs"][PROMPT_INPUT_NAME] = positive_prompt
    if NEGATIVE_NODE_ID and negative_prompt is not None:
        workflow[NEGATIVE_NODE_ID]["inputs"]["text"] = negative_prompt
    if voice:
        workflow[VOICE_NODE_ID]["inputs"][VOICE_INPUT_NAME] = voice

    client_id = str(uuid.uuid4())
    resp = requests.post(
        f"{COMFYUI_URL}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["prompt_id"]


def wait_for_render(prompt_id, timeout=RENDER_TIMEOUT_SECONDS):
    """Poll ComfyUI's history endpoint until the render completes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=30)
        resp.raise_for_status()
        history = resp.json()
        if prompt_id in history:
            outputs = history[prompt_id].get("outputs", {})
            node_output = outputs.get(SAVE_IMAGE_NODE_ID)
            if node_output and node_output.get(OUTPUT_HISTORY_KEY):
                return node_output[OUTPUT_HISTORY_KEY][0]  # {filename, subfolder, type}
            status = history[prompt_id].get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI reported an error for {prompt_id}: {status.get('messages')}")
        time.sleep(2)
    raise TimeoutError(f"Render {prompt_id} did not finish within {timeout}s")


def fetch_image_bytes(image_info):
    resp = requests.get(
        f"{COMFYUI_URL}/view",
        params={
            "filename": image_info["filename"],
            "subfolder": image_info.get("subfolder", ""),
            "type": image_info.get("type", "output"),
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content, image_info["filename"]


# ---- Email sending --------------------------------------------------------

def send_reply(to_addr, subject, in_reply_to, references, body_text, image_bytes, image_filename, error=None):
    msg = EmailMessage()
    msg["From"] = EMAIL_USER
    msg["To"] = to_addr
    if not subject.strip():
        # A bare "Re: " subject counts towards spam; give the reply a real one.
        msg["Subject"] = f"Your {OUTPUT_KIND}"
    else:
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    # Spam filters penalise a missing Date and a Message-ID on the machine's
    # bare hostname (e.g. "@magnor"), so set both explicitly.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=EMAIL_USER.rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    msg.set_content(body_text)

    if image_bytes:
        # SaveImage gives .png; SaveAudio/SaveAudioMP3 give .flac/.mp3 etc.
        mime = mimetypes.guess_type(image_filename)[0]
        if not mime:
            mime = "image/png" if OUTPUT_KIND == "image" else "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(
            image_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=image_filename,
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)


# ---- Main processing loop --------------------------------------------------

def process_message(client, uid, raw_message):
    import email

    msg = email.message_from_bytes(raw_message)
    from_addr = email.utils.parseaddr(msg.get("From", ""))[1].lower()
    # Decode RFC 2047 words (=?UTF-8?Q?...?=) so non-ASCII subjects read and match properly.
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    # Outlook folds these headers ("Message-ID:\r\n <...>"); unfold them, since
    # header values with line breaks can't be reused in the reply.
    message_id = " ".join(msg.get("Message-ID", "").split())
    references = " ".join(msg.get("References", "").split()) or message_id

    print(f"[{time.strftime('%H:%M:%S')}] New mail from {from_addr}: {subject!r}")

    if ALLOWED_SENDERS and from_addr not in ALLOWED_SENDERS:
        print(f"  -> sender not in ALLOWED_SENDERS, ignoring")
        return

    body = get_plain_text_body(msg)
    positive, negative = extract_prompts(body)

    if not positive:
        print("  -> email body was empty, sending an error reply")
        if OUTPUT_KIND == "audio":
            hint = ("Your email body was empty, so there was nothing to read aloud. "
                    "Write the text you want spoken as the message body.")
        else:
            hint = ("Your email body was empty, so there was nothing to use as a prompt. "
                    "Just write your prompt as the message body, or use:\n\nPrompt: a photo of a red fox in snow\nNegative: blurry, low quality")
        send_reply(from_addr, subject, message_id, references, hint, None, None)
        return

    try:
        voice, voice_note = pick_voice(subject) if VOICE_NODE_ID else (None, "")
        if voice_note:
            print(f"  -> {voice_note}")
        print(f"  -> submitting render: {positive!r}")
        prompt_id = submit_render(positive, negative, voice)
        image_info = wait_for_render(prompt_id)
        image_bytes, filename = fetch_image_bytes(image_info)
        print(f"  -> render complete: {filename}")
        reply = f"Here's your {'audio' if OUTPUT_KIND == 'audio' else 'image'} for:\n\n{positive}"
        if voice_note:
            reply += f"\n\n{voice_note}"
        send_reply(
            from_addr, subject, message_id, references,
            reply,
            image_bytes, filename,
        )
    except Exception as e:
        print(f"  -> render failed: {e}")
        traceback.print_exc()
        send_reply(
            from_addr, subject, message_id, references,
            f"Sorry, the render failed: {e}",
            None, None,
        )


def resolve_folder_path(client, name):
    """
    Some IMAP servers (one.com, other Dovecot-based hosts included) require
    custom folders to live under a personal namespace prefix, e.g. "INBOX."
    rather than at the top level. Detect that prefix via NAMESPACE and apply
    it, so the same code works whether or not a prefix is required.
    """
    if "." in name or "/" in name:
        return name  # already looks like a full path

    try:
        ns = client.namespace()
        personal = ns.personal
    except Exception:
        personal = None

    if personal:
        prefix, _sep = personal[0]
        if prefix and not name.startswith(prefix):
            return f"{prefix}{name}"
    return name


def ensure_processed_folder(client):
    global PROCESSED_FOLDER
    PROCESSED_FOLDER = resolve_folder_path(client, PROCESSED_FOLDER)
    folders = [f[2] for f in client.list_folders()]
    if PROCESSED_FOLDER not in folders:
        client.create_folder(PROCESSED_FOLDER)


def handle_new_messages(client):
    client.select_folder("INBOX")
    uids = client.search(["UNSEEN"])
    for uid in uids:
        raw = client.fetch([uid], ["RFC822"])[uid][b"RFC822"]
        try:
            process_message(client, uid, raw)
        except Exception:
            print("Unhandled error processing message:")
            traceback.print_exc()
        finally:
            # Mark seen and move aside so we never reprocess it.
            client.add_flags([uid], [b"\\Seen"])
            try:
                client.move([uid], PROCESSED_FOLDER)
            except Exception:
                pass  # some servers use copy+delete; not fatal if move fails


def main_loop():
    while True:
        try:
            with IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True) as client:
                client.login(EMAIL_USER, EMAIL_PASS)
                ensure_processed_folder(client)
                client.select_folder("INBOX")

                # Handle anything that arrived while we were offline.
                handle_new_messages(client)

                print(f"[{time.strftime('%H:%M:%S')}] Entering IDLE, watching {EMAIL_USER} on {IMAP_HOST} ({OUTPUT_KIND})...")
                while True:
                    client.idle()
                    responses = client.idle_check(timeout=IDLE_TIMEOUT_SECONDS)
                    client.idle_done()
                    if responses:
                        handle_new_messages(client)
        except Exception as e:
            print(f"Connection error: {e}. Reconnecting in 15s...")
            traceback.print_exc()
            time.sleep(15)


if __name__ == "__main__":
    main_loop()
