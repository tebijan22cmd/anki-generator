"""Flask server: serves the page, builds decks from a pasted reply, and talks to AnkiConnect."""

import json
import os
import re
import time
import urllib.request
import uuid

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from extractors import MAX_SOURCE_CHARS, ExtractError, extract_document, extract_youtube
from generator import DeckError, UploadedImage, build_deck

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

ANKI_CONNECT_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOC_BYTES = 25 * 1024 * 1024
MAX_REPLY_BYTES = 2 * 1024 * 1024
KEEP_DECKS_SECONDS = 24 * 3600
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = max(MAX_IMAGES * MAX_IMAGE_BYTES, MAX_DOC_BYTES) + MAX_REPLY_BYTES + 1024 * 1024


def _error(msg: str, status: int = 400):
    return jsonify({"error": msg}), status


# --------------------------------------------------------------------------- AnkiConnect

class AnkiError(Exception):
    """AnkiConnect could not be reached or refused the request."""


def anki_call(action: str, params: dict = None, timeout: float = 5):
    body = json.dumps({"action": action, "version": 6, "params": params or {}}).encode()
    req = urllib.request.Request(ANKI_CONNECT_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except ValueError:
        raise AnkiError("Anki sent back a reply that wasn't understood.")
    except OSError:
        raise AnkiError("Can't reach Anki. Open Anki and make sure the AnkiConnect add-on is installed.")
    if isinstance(data, dict) and data.get("error"):
        raise AnkiError(str(data["error"]))
    return data.get("result") if isinstance(data, dict) else data


@app.get("/api/anki-status")
def anki_status():
    try:
        return jsonify({"connected": True, "version": anki_call("version", timeout=1.5)})
    except AnkiError:
        return jsonify({"connected": False})


# --------------------------------------------------------------------------- pages and decks

def _cleanup_old_decks():
    cutoff = time.time() - KEEP_DECKS_SECONDS
    for name in os.listdir(OUTPUT_DIR):
        path = os.path.join(OUTPUT_DIR, name)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)


@app.get("/api/health")
def health():
    return jsonify({"app": "anki-generator", "pid": os.getpid()})


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.post("/api/extract")
def extract():
    """Read one uploaded document, or one YouTube link, and return its text."""
    url = (request.form.get("url") or "").strip()
    upload = request.files.get("file")
    try:
        if url:
            item = extract_youtube(url)
        elif upload:
            data = upload.read()
            if len(data) > MAX_DOC_BYTES:
                return _error(f"{upload.filename} is larger than 25 MB.")
            item = extract_document(upload.filename or "", data)
        else:
            return _error("Choose a file or paste a YouTube link.")
    except ExtractError as e:
        return _error(str(e), 422)
    except Exception:
        app.logger.exception("Extraction failed")
        return _error("Something went wrong while reading that source.", 500)

    return jsonify({"name": item.name, "kind": item.kind, "text": item.text, "chars": len(item.text),
                    "truncated": item.truncated, "note": item.note, "limit": MAX_SOURCE_CHARS})


@app.post("/api/build")
def build():
    """Build an .apkg from the pasted reply; optionally import it into the open Anki."""
    raw = (request.form.get("reply") or "").strip()
    if not raw:
        return _error("Paste the AI's reply first.")
    if len(raw.encode("utf-8")) > MAX_REPLY_BYTES:
        return _error("The pasted reply is too large.")
    try:
        reply = json.loads(raw)
    except ValueError:
        return _error("The pasted reply isn't valid JSON. Ask the AI to reply with only the JSON object.")

    deck_name = " ".join((request.form.get("deckName") or "").split())
    if len(deck_name) > 100:
        return _error("Deck name must be 100 characters or fewer.")

    files = request.files.getlist("images")
    if len(files) > MAX_IMAGES:
        return _error(f"Attach at most {MAX_IMAGES} images.")
    images = []
    for f in files:
        if f.mimetype not in ALLOWED_IMAGE_TYPES:
            return _error(f"{f.filename}: only PNG, JPEG, WebP and GIF images are supported.")
        data = f.read()
        if len(data) > MAX_IMAGE_BYTES:
            return _error(f"{f.filename} is larger than 10 MB.")
        images.append(UploadedImage(filename=f.filename or "image", data=data))

    _cleanup_old_decks()
    request_id = uuid.uuid4().hex
    try:
        result = build_deck(reply, images, OUTPUT_DIR, request_id, deck_name)
    except DeckError as e:
        return _error(str(e), 422)
    except Exception:
        app.logger.exception("Deck build failed")
        return _error("Something went wrong while building the deck.", 500)

    with open(os.path.join(OUTPUT_DIR, f"{request_id}.json"), "w") as f:
        json.dump({"filename": result.filename}, f)

    sent, anki_error = False, None
    if request.form.get("sendToAnki") == "true":
        try:
            if not anki_call("importPackage", {"path": os.path.abspath(result.path)}, timeout=120):
                raise AnkiError("Anki declined to import the deck.")
            sent = True
        except AnkiError as e:
            anki_error = str(e)

    return jsonify({
        "sent": sent,
        "ankiError": anki_error,
        "downloadUrl": f"/api/download/{request_id}",
        "filename": result.filename,
        "title": result.title,
        "counts": {"basic": result.basic, "cloze": result.cloze, "occlusion": result.occlusion},
        "warnings": result.warnings,
        "preview": result.preview,
    })


@app.get("/api/download/<deck_id>")
def download(deck_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", deck_id):
        abort(404)
    path = os.path.join(OUTPUT_DIR, f"{deck_id}.apkg")
    if not os.path.exists(path):
        return _error("This deck has expired or does not exist. Please build it again.", 404)
    try:
        with open(os.path.join(OUTPUT_DIR, f"{deck_id}.json")) as f:
            filename = json.load(f)["filename"]
    except (OSError, ValueError, KeyError):
        filename = "ukmla-deck.apkg"
    return send_file(path, as_attachment=True, download_name=filename, mimetype="application/octet-stream")


@app.errorhandler(413)
def too_large(_):
    return _error("The upload is too large. Images must be under 10 MB and documents under 25 MB.", 413)


if __name__ == "__main__":
    print(f"Anki Generator running at http://127.0.0.1:{os.environ.get('PORT', 8000)}", flush=True)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), debug=False, threaded=True)
