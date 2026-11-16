"""Pull plain text out of uploaded documents (PDF, DOCX, PPTX, TXT/MD) and YouTube transcripts."""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

MAX_SOURCE_CHARS = 150_000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class ExtractError(Exception):
    """An error whose message is safe to show to the user."""


@dataclass
class Extracted:
    name: str
    kind: str            # "pdf" | "docx" | "pptx" | "text" | "youtube"
    text: str
    truncated: bool = False
    note: str = ""


def _finish(name: str, kind: str, text: str, note: str = "") -> Extracted:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ExtractError(f"No text could be found in {name}.")
    truncated = len(text) > MAX_SOURCE_CHARS
    if truncated:
        cut = text[:MAX_SOURCE_CHARS]
        nl = cut.rfind("\n", MAX_SOURCE_CHARS - 2000)
        text = cut[: nl if nl > 0 else MAX_SOURCE_CHARS].rstrip()
    return Extracted(name=name, kind=kind, text=text, truncated=truncated, note=note)


# --------------------------------------------------------------------------- documents

def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractError("This PDF is password-protected.")
        pages = []
        for i, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if t:
                pages.append(f"[Page {i}]\n{t}")
    except ExtractError:
        raise
    except Exception:
        raise ExtractError("This PDF couldn't be read. It may be damaged.")
    if not pages:
        raise ExtractError("This PDF has no selectable text (it may be scanned images). Run it through OCR first.")
    return "\n\n".join(pages)


def _docx(data: bytes) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    try:
        doc = Document(io.BytesIO(data))
        lines = []
        for child in doc.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                p = Paragraph(child, doc)
                text = p.text.strip()
                if not text:
                    continue
                style = p.style.name if p.style is not None and p.style.name else ""
                lines.append(("## " + text) if style.startswith("Heading") or style == "Title" else text)
            elif tag == "tbl":
                for row in Table(child, doc).rows:
                    seen, cells = set(), []
                    for cell in row.cells:        # merged cells repeat; keep each once
                        if id(cell._tc) not in seen:
                            seen.add(id(cell._tc))
                            cells.append(cell.text.strip().replace("\n", " "))
                    if any(cells):
                        lines.append(" | ".join(cells))
    except Exception:
        raise ExtractError("This Word file couldn't be read. It may be damaged or not a real .docx.")
    return "\n".join(lines)


def _pptx(data: bytes) -> str:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def shape_lines(shapes):
        for sh in shapes:
            try:
                is_group = sh.shape_type == MSO_SHAPE_TYPE.GROUP
            except NotImplementedError:
                is_group = False
            if is_group:
                yield from shape_lines(sh.shapes)
            elif getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        yield " | ".join(cells)
            elif getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                t = sh.text_frame.text.strip()
                if t:
                    yield t

    try:
        prs = Presentation(io.BytesIO(data))
        slides = []
        for i, slide in enumerate(prs.slides, 1):
            parts = list(shape_lines(slide.shapes))
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
                if notes:
                    parts.append("Speaker notes: " + notes)
            if parts:
                slides.append(f"[Slide {i}]\n" + "\n".join(parts))
    except Exception:
        raise ExtractError("This PowerPoint file couldn't be read. It may be damaged or not a real .pptx.")
    return "\n\n".join(slides)


def _plain(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeError:
            pass
    return data.decode("latin-1")


DOCUMENT_TYPES = {".pdf": ("pdf", _pdf), ".docx": ("docx", _docx), ".pptx": ("pptx", _pptx),
                  ".txt": ("text", _plain), ".md": ("text", _plain)}


def extract_document(filename: str, data: bytes) -> Extracted:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".doc", ".ppt"):
        raise ExtractError(f"Old {ext} files aren't supported. Re-save it as {ext}x (or PDF) and try again.")
    if ext not in DOCUMENT_TYPES:
        raise ExtractError(f"{filename or 'This file'}: unsupported type. Use PDF, DOCX, PPTX, TXT or MD.")
    kind, reader = DOCUMENT_TYPES[ext]
    return _finish(filename, kind, reader(data))


# --------------------------------------------------------------------------- YouTube

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def _youtube_url(url: str) -> str:
    """Validate a YouTube link and rebuild it from the video ID, so only youtube.com URLs are ever fetched."""
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise ExtractError("That doesn't look like a YouTube link.")
    segs = [s for s in u.path.split("/") if s]
    if (u.hostname or "").lower().endswith("youtu.be"):
        vid = segs[0] if segs else ""
    elif segs[:1] == ["watch"]:
        vid = (parse_qs(u.query).get("v") or [""])[0]
    elif len(segs) >= 2 and segs[0] in ("shorts", "embed", "live", "v"):
        vid = segs[1]
    else:
        vid = ""
    if not VIDEO_ID.fullmatch(vid):
        raise ExtractError("Couldn't find a video ID in that link. Use a link to a single video.")
    return f"https://www.youtube.com/watch?v={vid}"


def _yt_dlp_path() -> str:
    for p in (os.environ.get("YT_DLP"), os.path.join(BASE_DIR, "bin", "yt-dlp"), shutil.which("yt-dlp")):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise ExtractError("yt-dlp isn't installed on the server (expected at bin/yt-dlp).")


def _run_yt_dlp(args: list, cwd: Optional[str] = None, timeout: int = 90) -> str:
    cmd = [_yt_dlp_path(), "--ignore-config", "--no-warnings", "--socket-timeout", "20", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise ExtractError("YouTube took too long to respond. Try again.")
    except OSError:
        raise ExtractError("yt-dlp couldn't be started on the server.")
    if proc.returncode != 0:
        lines = [l for l in proc.stderr.splitlines() if l.startswith("ERROR")]
        msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]*:\s*)?", "", lines[-1] if lines else "").strip()
        if "429" in msg:
            raise ExtractError("YouTube is refusing the caption download for this video right now (rate limit). "
                               "Try again later, or paste the transcript into the subject box.")
        raise ExtractError(f"YouTube said: {msg[:200]}" if msg else "yt-dlp failed to read that video.")
    return proc.stdout


def _pick_track(info: dict) -> Tuple[str, bool]:
    """Choose one caption track. Returns (language key, is_auto_generated)."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    english = ["en", "en-orig", "en-GB", "en-US", "en-CA", "en-AU", "en-IN"]
    for lang in english:
        if lang in manual:
            return lang, False
    for lang in english:
        if lang in auto:
            return lang, True
    if manual:
        return next(iter(manual)), False
    for lang in auto:                       # original-language auto captions are marked "-orig"
        if lang.endswith("-orig"):
            return lang, True
    raise ExtractError("This video has no captions, so there's no transcript to use.")


def _parse_json3(raw: str) -> str:
    paragraphs, current, last_end = [], [], None
    for ev in json.loads(raw).get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = re.sub(r"\s+", " ", "".join(s.get("utf8", "") for s in segs)).strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0)
        if last_end is not None and start - last_end > 3000 and current:   # a pause starts a new paragraph
            paragraphs.append(" ".join(current))
            current = []
        current.append(text)
        last_end = start + ev.get("dDurationMs", 0)
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _parse_vtt(raw: str) -> str:
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and (not out or out[-1] != line):      # auto captions repeat lines
            out.append(line)
    return " ".join(out)


def _caption_file(tmp: str) -> Optional[str]:
    files = sorted(os.listdir(tmp))
    return next((f for f in files if f.endswith(".json3")), None) or next((f for f in files if f.endswith(".vtt")), None)


def extract_youtube(url: str) -> Extracted:
    clean = _youtube_url(url)
    common = ["--skip-download", "--no-playlist", "--sub-format", "json3/vtt", "-o", "%(id)s"]
    with tempfile.TemporaryDirectory() as tmp:
        # Fast path, one request: English captions (manual preferred) plus the video's metadata.
        _run_yt_dlp([*common, "--write-subs", "--write-auto-subs", "--sub-langs", "en", "--write-info-json", "--", clean], cwd=tmp)
        info_path = next((f for f in os.listdir(tmp) if f.endswith(".info.json")), None)
        try:
            with open(os.path.join(tmp, info_path or ""), encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            raise ExtractError("yt-dlp couldn't read that video's details.")
        if info.get("is_live"):
            raise ExtractError("That's a live stream, which doesn't have a transcript yet.")

        path = _caption_file(tmp)
        is_auto = "en" not in (info.get("subtitles") or {})
        if not path:
            # No plain "en" track: choose the best available track from the metadata and fetch just that one.
            lang, is_auto = _pick_track(info)
            _run_yt_dlp([*common, "--write-auto-subs" if is_auto else "--write-subs", "--sub-langs", lang, "--", clean], cwd=tmp)
            path = _caption_file(tmp)
        if not path:
            raise ExtractError("YouTube didn't provide the captions for that video.")
        with open(os.path.join(tmp, path), encoding="utf-8", errors="replace") as f:
            raw = f.read()
    try:
        transcript = _parse_json3(raw) if path.endswith(".json3") else _parse_vtt(raw)
    except ValueError:
        raise ExtractError("The captions for that video couldn't be read.")

    title = (info.get("title") or "Untitled video").strip()
    header = f"Title: {title}\nChannel: {info.get('channel') or info.get('uploader') or 'unknown'}\nURL: {clean}\n\nTranscript:\n"
    note = "auto-generated captions, so expect some mis-heard terms" if is_auto else ""
    return _finish(f"YouTube: {title}"[:120], "youtube", header + transcript, note)
