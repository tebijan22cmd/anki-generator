"""Turns a pasted AI reply (JSON) plus the user's images into an Anki .apkg file."""

import hashlib
import html
import io
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import genanki
from PIL import Image, ImageDraw, ImageOps

MAX_IMAGE_EDGE = 1600      # uploaded images are downscaled to this before cards are drawn
MAX_TEXT_CARDS = 500
MAX_OCCLUSIONS_PER_IMAGE = 30


class DeckError(Exception):
    """An error whose message is safe to show to the user."""


# --------------------------------------------------------------------------- images

def _prepare_image(data: bytes) -> Tuple[Image.Image, str]:
    """Load, orient and downscale an uploaded image. Returns (image, extension)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise DeckError("One of the uploaded files is not a readable image.")

    source_format = img.format
    img = ImageOps.exif_transpose(img) or img
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        img = bg
    else:
        img = img.convert("RGB")
    return img, ("jpg" if source_format == "JPEG" else "png")


def _encode(img: Image.Image, ext: str) -> bytes:
    buf = io.BytesIO()
    if ext == "jpg":
        img.save(buf, "JPEG", quality=88)
    else:
        img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


MASK_FILL = (255, 235, 162)       # pale yellow, like Anki's built-in image occlusion
MASK_OUTLINE = (33, 33, 33)
TARGET_FILL = (255, 120, 120)     # the one being asked about
REVEAL_OUTLINE = (220, 30, 30)


def _boxes(occlusions: List[dict], w: int, h: int, pad: int = 3) -> List[Optional[Tuple[int, int, int, int]]]:
    """Convert boxes given in thousandths of the image size to pixel boxes (None if unusable)."""
    out = []
    for o in occlusions:
        try:
            x, y, bw, bh = (max(0.0, min(1000.0, float(o[k]))) for k in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            out.append(None)
            continue
        x0 = max(0, round(x / 1000 * w) - pad)
        y0 = max(0, round(y / 1000 * h) - pad)
        x1 = min(w, round((x + bw) / 1000 * w) + pad)
        y1 = min(h, round((y + bh) / 1000 * h) + pad)
        out.append((x0, y0, x1, y1) if x1 - x0 > 2 * pad + 2 and y1 - y0 > 2 * pad + 2 else None)
    return out


def _render(img: Image.Image, boxes: list, target: int, reveal: bool) -> Image.Image:
    """Hide-all, guess-one: every box masked; the target is red on the front, uncovered on the back."""
    out = img.copy()
    draw = ImageDraw.Draw(out)
    line = max(2, round(max(img.size) / 400))
    for i, box in enumerate(boxes):
        if box is None:
            continue
        if i == target:
            if reveal:
                draw.rectangle(box, outline=REVEAL_OUTLINE, width=line + 1)
            else:
                draw.rectangle(box, fill=TARGET_FILL, outline=MASK_OUTLINE, width=line)
        else:
            draw.rectangle(box, fill=MASK_FILL, outline=MASK_OUTLINE, width=line)
    return out


# --------------------------------------------------------------------------- Anki models

CARD_CSS = """
.card { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; font-size: 20px;
        text-align: center; color: #1d2330; background: #fff; line-height: 1.45; padding: 12px; }
.nightMode .card, .card.nightMode { color: #e7e9ee; background: #1d2128; }
.extra { margin-top: 18px; font-size: 16px; color: #6b7280; text-align: left; }
.area { margin-top: 14px; font-size: 12px; color: #9aa3b2; text-transform: uppercase; letter-spacing: .05em; }
.cloze { font-weight: bold; color: #2f6fed; }
.nightMode .cloze { color: #7aa2ff; }
.io-q { font-weight: 600; margin-bottom: 10px; }
.io-a { font-size: 22px; font-weight: bold; color: #c53030; margin-top: 10px; }
img { max-width: 100%; height: auto; }
"""

# Fixed IDs so re-importing later decks reuses the same note types instead of duplicating them.
BASIC_MODEL = genanki.Model(
    1764213901, "UKMLA Basic",
    fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Extra"}, {"name": "UKMLA Area"}],
    templates=[{
        "name": "Card 1",
        "qfmt": "{{Front}}",
        "afmt": '{{FrontSide}}<hr id="answer">{{Back}}'
                '{{#Extra}}<div class="extra">{{Extra}}</div>{{/Extra}}'
                '<div class="area">{{UKMLA Area}}</div>',
    }],
    css=CARD_CSS,
)

CLOZE_MODEL = genanki.Model(
    1764213902, "UKMLA Cloze",
    fields=[{"name": "Text"}, {"name": "Extra"}, {"name": "UKMLA Area"}],
    templates=[{
        "name": "Cloze",
        "qfmt": "{{cloze:Text}}",
        "afmt": '{{cloze:Text}}{{#Extra}}<div class="extra">{{Extra}}</div>{{/Extra}}'
                '<div class="area">{{UKMLA Area}}</div>',
    }],
    css=CARD_CSS,
    model_type=genanki.Model.CLOZE,
)

OCCLUSION_MODEL = genanki.Model(
    1764213903, "UKMLA Image Occlusion",
    fields=[{"name": "Question"}, {"name": "Front Image"}, {"name": "Back Image"},
            {"name": "Answer"}, {"name": "Explanation"}, {"name": "UKMLA Area"}],
    templates=[{
        "name": "Occlusion",
        "qfmt": '<div class="io-q">{{Question}}</div>{{Front Image}}',
        "afmt": '<div class="io-q">{{Question}}</div>{{Back Image}}'
                '<div class="io-a">{{Answer}}</div>'
                '{{#Explanation}}<div class="extra">{{Explanation}}</div>{{/Explanation}}'
                '<div class="area">{{UKMLA Area}}</div>',
    }],
    css=CARD_CSS,
)


# --------------------------------------------------------------------------- reply parsing helpers

def _s(v, limit: int = 4000) -> str:
    return str(v).strip()[:limit] if isinstance(v, (str, int, float)) else ""


def _esc(s: str) -> str:
    # html.escape leaves {{c1::...}} cloze markers intact.
    return html.escape(s or "").replace("\n", "<br>")


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _tags(area: str, tags) -> List[str]:
    out = []
    if area:
        out.append("UKMLA::" + re.sub(r"[^A-Za-z0-9]+", "_", area).strip("_"))
    for t in _list(tags)[:6]:
        t = re.sub(r"[^\w\-:]+", "-", _s(t, 40).lower()).strip("-")
        if t:
            out.append(t)
    return out


CLOZE_RE = re.compile(r"\{\{c\d+::.+?\}\}")


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return (s[:60] or "ukmla-deck").lower()


def _deck_id(title: str) -> int:
    """Stable per deck name, so sending the same name again adds to the same deck in Anki."""
    return int(hashlib.sha1(title.encode("utf-8")).hexdigest()[:8], 16) % (1 << 30) + (1 << 30)


# --------------------------------------------------------------------------- orchestration

@dataclass
class UploadedImage:
    filename: str
    data: bytes


@dataclass
class DeckResult:
    path: str
    filename: str
    title: str
    basic: int = 0
    cloze: int = 0
    occlusion: int = 0
    warnings: List[str] = field(default_factory=list)
    preview: List[dict] = field(default_factory=list)


def build_deck(reply: dict, images: List[UploadedImage], out_dir: str, request_id: str,
               deck_name: str = "") -> DeckResult:
    if not isinstance(reply, dict):
        raise DeckError("The reply must be a single JSON object.")

    title = " ".join(deck_name.split()) or _s(reply.get("deck_title"), 100) or "UKMLA Deck"
    deck = genanki.Deck(_deck_id(title), title)
    result = DeckResult(path="", filename=_slug(title) + ".apkg", title=title)

    # ---- text cards
    basic_raw = _list(reply.get("basic_cards"))
    cloze_raw = _list(reply.get("cloze_cards"))
    if len(basic_raw) + len(cloze_raw) > MAX_TEXT_CARDS:
        raise DeckError(f"The reply has more than {MAX_TEXT_CARDS} text cards. Ask for fewer.")

    skipped = 0
    for c in basic_raw:
        c = c if isinstance(c, dict) else {}
        front, back = _s(c.get("front")), _s(c.get("back"))
        if not front or not back:
            skipped += 1
            continue
        area = _s(c.get("ukmla_area"), 80)
        deck.add_note(genanki.Note(
            model=BASIC_MODEL,
            fields=[_esc(front), _esc(back), _esc(_s(c.get("extra"))), _esc(area)],
            tags=_tags(area, c.get("tags")),
        ))
        result.basic += 1
        if result.basic <= 3:
            result.preview.append({"type": "Basic", "front": front, "back": back})

    for c in cloze_raw:
        c = c if isinstance(c, dict) else {}
        text = _s(c.get("text"))
        if not CLOZE_RE.search(text):
            skipped += 1
            continue
        area = _s(c.get("ukmla_area"), 80)
        deck.add_note(genanki.Note(
            model=CLOZE_MODEL,
            fields=[_esc(text), _esc(_s(c.get("extra"))), _esc(area)],
            tags=_tags(area, c.get("tags")),
        ))
        result.cloze += 1
        if result.cloze <= 3:
            result.preview.append({"type": "Cloze", "front": text, "back": _s(c.get("extra"))})

    if skipped:
        result.warnings.append(
            f"{skipped} text card(s) were skipped because they were incomplete "
            "(missing a front/back, or a cloze card with no {{c1::...}} blank)."
        )

    # ---- image occlusion cards
    media_files: List[str] = []
    prepared: Dict[int, Tuple[Image.Image, str]] = {}
    for entry in _list(reply.get("images")):
        entry = entry if isinstance(entry, dict) else {}
        try:
            n = int(entry.get("image_number"))
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= len(images):
            result.warnings.append(
                f"The reply has occlusions for image {n or '?'}, but only {len(images)} image(s) were attached "
                "on this page. Add the images in the same order you gave the AI."
            )
            continue
        if n not in prepared:
            prepared[n] = _prepare_image(images[n - 1].data)
        img, ext = prepared[n]
        name = images[n - 1].filename

        occ = [o for o in _list(entry.get("occlusions")) if isinstance(o, dict)][:MAX_OCCLUSIONS_PER_IMAGE]
        boxes = _boxes(occ, img.width, img.height)
        valid = [j for j, b in enumerate(boxes) if b is not None]
        if len(valid) < len(occ):
            result.warnings.append(f"{len(occ) - len(valid)} occlusion(s) on {name} had unusable coordinates and were skipped.")
        if not valid:
            continue

        area = _s(entry.get("ukmla_area"), 80)
        for j in valid:
            o = occ[j]
            names = []
            for side, reveal in (("q", False), ("a", True)):
                fname = f"{request_id}_img{n}_{j}_{side}.{ext}"
                path = os.path.join(out_dir, fname)
                with open(path, "wb") as f:
                    f.write(_encode(_render(img, boxes, j, reveal), ext))
                media_files.append(path)
                names.append(fname)
            question = _s(o.get("question")) or "Identify the highlighted structure"
            deck.add_note(genanki.Note(
                model=OCCLUSION_MODEL,
                fields=[
                    _esc(question),
                    f'<img src="{names[0]}">',
                    f'<img src="{names[1]}">',
                    _esc(_s(o.get("answer"))),
                    _esc(_s(o.get("explanation"))),
                    _esc(area),
                ],
                tags=_tags(area, ["image-occlusion"]),
            ))
            result.occlusion += 1
        first = occ[valid[0]]
        result.preview.append({"type": "Image occlusion", "front": f"{name}: {_s(first.get('question'))}",
                               "back": _s(first.get("answer"))})

    try:
        if result.basic + result.cloze + result.occlusion == 0:
            raise DeckError("No usable cards were found in the reply.")
        result.path = os.path.join(out_dir, f"{request_id}.apkg")
        genanki.Package(deck, media_files=media_files).write_to_file(result.path)
    finally:
        for p in media_files:
            if os.path.exists(p):
                os.remove(p)  # they're now inside the .apkg (or the build failed)
    return result
