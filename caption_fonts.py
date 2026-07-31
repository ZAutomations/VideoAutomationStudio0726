"""Bundled + user caption fonts — ported from HarisClipper's clipper-tool.

Two core fonts (Roboto Regular/Bold) are fetched synchronously on first use so
default captions always render. A curated set of ~20 trending display fonts
(the kind used in viral shorts) plus multilingual fonts are then fetched in the
background. Everything lands in ``assets/fonts/`` which is handed to libass via
``fontsdir`` when burning ASS captions with ffmpeg.

Users can also drop their own .ttf/.otf into ``assets/fonts/``; the family name
is read straight from the font's ``name`` table (no extra dependency).
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
from pathlib import Path
from typing import BinaryIO, Optional

logger = logging.getLogger(__name__)

# Fonts directory — same ``assets/fonts/`` the clipper-tool uses.
_FONTS_DIR = Path(os.environ.get("CHANGEGUI_ROOT", Path(__file__).resolve().parent)) / "assets" / "fonts"

_GF = "https://raw.githubusercontent.com/google/fonts/main"

# --------------------------------------------------------------------------- #
# Core fonts — downloaded synchronously; default captions need them.
# --------------------------------------------------------------------------- #
_CORE_FONTS = {
    "Roboto-Regular.ttf": (
        "https://github.com/googlefonts/roboto-2/raw/main/src/hinted/Roboto-Regular.ttf"
    ),
    "Roboto-Bold.ttf": (
        "https://github.com/googlefonts/roboto-2/raw/main/src/hinted/Roboto-Bold.ttf"
    ),
}

# --------------------------------------------------------------------------- #
# Trending display fonts — family name (as embedded / used in ASS Fontname)
# -> (local filename, download url).
# --------------------------------------------------------------------------- #
TRENDING_FONTS: dict[str, tuple[str, str]] = {
    "Anton": ("Anton-Regular.ttf", f"{_GF}/ofl/anton/Anton-Regular.ttf"),
    "Bebas Neue": ("BebasNeue-Regular.ttf", f"{_GF}/ofl/bebasneue/BebasNeue-Regular.ttf"),
    "Archivo Black": ("ArchivoBlack-Regular.ttf", f"{_GF}/ofl/archivoblack/ArchivoBlack-Regular.ttf"),
    "Poppins": ("Poppins-Bold.ttf", f"{_GF}/ofl/poppins/Poppins-Bold.ttf"),
    "Montserrat": ("Montserrat.ttf", f"{_GF}/ofl/montserrat/Montserrat%5Bwght%5D.ttf"),
    "Oswald": ("Oswald.ttf", f"{_GF}/ofl/oswald/Oswald%5Bwght%5D.ttf"),
    "Teko": ("Teko.ttf", f"{_GF}/ofl/teko/Teko%5Bwght%5D.ttf"),
    "Changa": ("Changa.ttf", f"{_GF}/ofl/changa/Changa%5Bwght%5D.ttf"),
    "Bangers": ("Bangers-Regular.ttf", f"{_GF}/ofl/bangers/Bangers-Regular.ttf"),
    "Luckiest Guy": ("LuckiestGuy-Regular.ttf", f"{_GF}/apache/luckiestguy/LuckiestGuy-Regular.ttf"),
    "Permanent Marker": ("PermanentMarker-Regular.ttf", f"{_GF}/apache/permanentmarker/PermanentMarker-Regular.ttf"),
    "Alfa Slab One": ("AlfaSlabOne-Regular.ttf", f"{_GF}/ofl/alfaslabone/AlfaSlabOne-Regular.ttf"),
    "Russo One": ("RussoOne-Regular.ttf", f"{_GF}/ofl/russoone/RussoOne-Regular.ttf"),
    "Titan One": ("TitanOne-Regular.ttf", f"{_GF}/ofl/titanone/TitanOne-Regular.ttf"),
    "Paytone One": ("PaytoneOne-Regular.ttf", f"{_GF}/ofl/paytoneone/PaytoneOne-Regular.ttf"),
    "Lilita One": ("LilitaOne-Regular.ttf", f"{_GF}/ofl/lilitaone/LilitaOne-Regular.ttf"),
    "Passion One": ("PassionOne-Bold.ttf", f"{_GF}/ofl/passionone/PassionOne-Bold.ttf"),
    "Sigmar One": ("SigmarOne-Regular.ttf", f"{_GF}/ofl/sigmarone/SigmarOne-Regular.ttf"),
    "Bowlby One SC": ("BowlbyOneSC-Regular.ttf", f"{_GF}/ofl/bowlbyonesc/BowlbyOneSC-Regular.ttf"),
    "Concert One": ("ConcertOne-Regular.ttf", f"{_GF}/ofl/concertone/ConcertOne-Regular.ttf"),
    "Bungee": ("Bungee-Regular.ttf", f"{_GF}/ofl/bungee/Bungee-Regular.ttf"),
    "Shrikhand": ("Shrikhand-Regular.ttf", f"{_GF}/ofl/shrikhand/Shrikhand-Regular.ttf"),
    "DM Serif Display": ("DMSerifDisplay-Regular.ttf", f"{_GF}/ofl/dmserifdisplay/DMSerifDisplay-Regular.ttf"),
}

# --------------------------------------------------------------------------- #
# Multilingual fonts — for non-Latin captions (Urdu / Hindi / Arabic).
# --------------------------------------------------------------------------- #
MULTILINGUAL_FONTS: dict[str, tuple[str, str]] = {
    "Noto Nastaliq Urdu": (
        "NotoNastaliqUrdu-Regular.ttf",
        f"{_GF}/ofl/notonastaliqurdu/NotoNastaliqUrdu%5Bwght%5D.ttf",
    ),
    "Noto Naskh Arabic": (
        "NotoNaskhArabic-Regular.ttf",
        f"{_GF}/ofl/notonaskharabic/NotoNaskhArabic%5Bwght%5D.ttf",
    ),
    "Noto Sans Arabic": (
        "NotoSansArabic-Regular.ttf",
        f"{_GF}/ofl/notosansarabic/NotoSansArabic%5Bwdth%2Cwght%5D.ttf",
    ),
    "Noto Sans Devanagari": (
        "NotoSansDevanagari-Regular.ttf",
        f"{_GF}/ofl/notosansdevanagari/NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf",
    ),
    "Noto Serif Devanagari": (
        "NotoSerifDevanagari-Regular.ttf",
        f"{_GF}/ofl/notoserifdevanagari/NotoSerifDevanagari%5Bwdth%2Cwght%5D.ttf",
    ),
}

# --------------------------------------------------------------------------- #
# Per-language default font (so picking the language auto-swaps the typeface).
# --------------------------------------------------------------------------- #
LANG_DEFAULT_FONT: dict[str, str] = {
    "ur": "Noto Nastaliq Urdu",
    "ar": "Noto Sans Arabic",
    "fa": "Noto Naskh Arabic",
    "hi": "Noto Sans Devanagari",
    "mr": "Noto Sans Devanagari",
    "ne": "Noto Sans Devanagari",
}

ALLOWED_FONT_EXTS = {".ttf", ".otf"}


# --------------------------------------------------------------------------- #
# Download helpers
# --------------------------------------------------------------------------- #
def _download(filename: str, url: str) -> bool:
    """Fetch one font into FONTS_DIR if missing. Returns True if present after."""
    target = _FONTS_DIR / filename
    if target.exists() and target.stat().st_size > 0:
        return True
    try:
        import requests
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        target.write_bytes(resp.content)
        logger.info("Saved font %s (%d bytes).", filename, len(resp.content))
        return True
    except Exception as exc:
        logger.warning("Could not download font %s (%s).", filename, exc)
        return False


def _download_all(fonts: dict[str, tuple[str, str]]) -> None:
    """Download a dict of family->(file, url) fonts."""
    _FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in fonts.values():
        _download(filename, url)


def ensure_core_fonts() -> None:
    """Download the core Roboto fonts (synchronous; needed for default render)."""
    _FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in _CORE_FONTS.items():
        _download(filename, url)


def ensure_trending_fonts() -> None:
    """Download trending + multilingual fonts in the background."""
    _FONTS_DIR.mkdir(parents=True, exist_ok=True)
    _download_all(TRENDING_FONTS)
    _download_all(MULTILINGUAL_FONTS)
    logger.info("Trending + multilingual fonts ready in %s", _FONTS_DIR)


def ensure_fonts() -> None:
    """Core fonts now (blocking), trending fonts in the background."""
    ensure_core_fonts()
    threading.Thread(target=ensure_trending_fonts, daemon=True).start()


def get_fonts_dir() -> Path:
    """Return the fonts directory (creating it if needed)."""
    _FONTS_DIR.mkdir(parents=True, exist_ok=True)
    return _FONTS_DIR


# --------------------------------------------------------------------------- #
# Font family extraction (for user uploads) — minimal sfnt 'name' table reader
# --------------------------------------------------------------------------- #
def _family_from_sfnt(data: bytes) -> Optional[str]:
    """Read the family name from a TTF/OTF byte string, or None on any problem.

    Prefers the Typographic Family (nameID 16), falling back to Family (nameID 1).
    Handles TrueType collections by reading the first font.
    """
    try:
        offset = 0
        if data[:4] == b"ttcf":
            offset = struct.unpack(">I", data[12:16])[0]
        num_tables = struct.unpack(">H", data[offset + 4: offset + 6])[0]
        name_off = None
        rec = offset + 12
        for _ in range(num_tables):
            tag = data[rec: rec + 4]
            toff = struct.unpack(">I", data[rec + 8: rec + 12])[0]
            if tag == b"name":
                name_off = toff
                break
            rec += 16
        if name_off is None:
            return None
        count, string_off = struct.unpack(">HH", data[name_off + 2: name_off + 6])
        strings = name_off + string_off
        family_1 = None
        for i in range(count):
            r = name_off + 6 + i * 12
            platform, _enc, _lang, name_id, length, off = struct.unpack(
                ">HHHHHH", data[r: r + 12]
            )
            if name_id not in (1, 16):
                continue
            raw = data[strings + off: strings + off + length]
            try:
                text = (
                    raw.decode("utf-16-be") if platform in (0, 3) else raw.decode("latin-1")
                ).strip()
            except Exception:
                continue
            if not text:
                continue
            if name_id == 16:
                return text
            if family_1 is None:
                family_1 = text
        return family_1
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Font listing
# --------------------------------------------------------------------------- #
def list_fonts() -> dict:
    """Return the available fonts, split into bundled and user-uploaded.

    Each entry is ``{"family", "file"}``.
    """
    bundled = [{"family": "Roboto", "file": fn} for fn in _CORE_FONTS]
    bundled += [{"family": fam, "file": fn} for fam, (fn, _url) in TRENDING_FONTS.items()]
    multilingual = [
        {"family": fam, "file": fn} for fam, (fn, _url) in MULTILINGUAL_FONTS.items()
    ]

    # User fonts: any .ttf/.otf in the fonts dir that isn't in our known lists
    _FONTS_DIR.mkdir(parents=True, exist_ok=True)
    known_files = set(_CORE_FONTS.keys())
    known_files |= {fn for fn, _url in list(TRENDING_FONTS.values())}
    known_files |= {fn for fn, _url in list(MULTILINGUAL_FONTS.values())}
    user: list[dict] = []
    for f in _FONTS_DIR.iterdir():
        if f.suffix.lower() in ALLOWED_FONT_EXTS and f.name not in known_files:
            family = _family_from_sfnt(f.read_bytes()) or f.stem
            user.append({"family": family, "file": f.name})

    return {
        "bundled": bundled,
        "multilingual": multilingual,
        "user": user,
    }


def font_path_for_family(family: str) -> Optional[Path]:
    """Return the full path to a font file for the given family name, or None."""
    fonts = list_fonts()
    all_fonts = fonts["bundled"] + fonts["multilingual"] + fonts["user"]
    for entry in all_fonts:
        if entry["family"].lower() == family.lower():
            return _FONTS_DIR / entry["file"]
    return None


def resolve_font_path(font_style: str) -> Optional[Path]:
    """Resolve a font style string (display family name OR a .ttf/.otf filename)
    to a real font file on disk, or None if nothing can be found.

    Priority:
      1. Bundled fonts (``assets/fonts/``) — matches a family name from
         TRENDING_FONTS / MULTILINGUAL_FONTS / CORE_FONTS (downloading it on
         demand if the background download hasn't finished yet), then a bare
         filename inside the bundled fonts dir.
      2. Windows system fonts (``C:\\Windows\\Fonts``) — matches a filename.

    This lets the caption renderers accept the same values the GUI dropdowns
    expose (e.g. ``"Luckiest Guy"`` or ``"LuckiestGuy-Regular.ttf"``) instead of
    silently falling back to Arial.
    """
    if not font_style:
        return None
    style = str(font_style).strip()
    if not style:
        return None

    # 1a. Bundled family name → ensure downloaded, return file path.
    _CORE_FAMILIES = {
        "Roboto": "Roboto-Regular.ttf",
        "Roboto Bold": "Roboto-Bold.ttf",
    }
    if style in _CORE_FAMILIES:
        _path = _FONTS_DIR / _CORE_FAMILIES[style]
        if not _path.exists():
            ensure_core_fonts()
        return _path if _path.exists() else None
    if style in TRENDING_FONTS:
        fn, _url = TRENDING_FONTS[style]
        _path = _FONTS_DIR / fn
        if not _path.exists():
            download_font_family(style)
        return _path if _path.exists() else None
    if style in MULTILINGUAL_FONTS:
        fn, _url = MULTILINGUAL_FONTS[style]
        _path = _FONTS_DIR / fn
        if not _path.exists():
            download_font_family(style)
        return _path if _path.exists() else None
    if style in _CORE_FONTS:
        _path = _FONTS_DIR / style
        if not _path.exists():
            ensure_core_fonts()
        return _path if _path.exists() else None

    # 1b. Bare filename inside the bundled fonts dir (e.g. "Anton-Regular.ttf").
    _FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ALLOWED_FONT_EXTS:
        cand = _FONTS_DIR / style if style.lower().endswith(ext) else _FONTS_DIR / (style + ext)
        if cand.exists():
            return cand

    # 2. Windows system font by filename.
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for ext in ALLOWED_FONT_EXTS:
        cand = win / style if style.lower().endswith(ext) else win / (style + ext)
        if cand.exists():
            return cand

    return None


def is_font_available(family: str) -> bool:
    """Return True if the font family is already downloaded."""
    return font_path_for_family(family) is not None


def download_font_family(family: str) -> bool:
    """Download a specific trending/multilingual font family. Returns True on success."""
    if family in TRENDING_FONTS:
        fn, url = TRENDING_FONTS[family]
        return _download(fn, url)
    if family in MULTILINGUAL_FONTS:
        fn, url = MULTILINGUAL_FONTS[family]
        return _download(fn, url)
    logger.warning("Unknown font family '%s' — not in trending or multilingual lists.", family)
    return False


# --------------------------------------------------------------------------- #
# Lazy init — download core fonts on first import if not present
# --------------------------------------------------------------------------- #
if not (_FONTS_DIR / "Roboto-Regular.ttf").exists():
    try:
        ensure_core_fonts()
    except Exception:
        pass  # will be retried on first render