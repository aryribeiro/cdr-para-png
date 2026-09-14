"""Corrige a posição e a largura do texto artístico que a libcdr converte torto.

Medido (14/09/2026) contra a miniatura que o próprio CorelDRAW grava dentro do
.cdr (`metadata/thumbnails/page1.bmp`), num crachá do CorelDRAW X5:

1. LARGURA. Para texto artístico a libcdr DOBRA a largura da caixa antes de
   emiti-la (`CDRContentCollector::collectArtisticText`: `m_currentBBox.m_w
   *= 2.0`). Logo a largura que o CorelDRAW mediu para aquele texto, com a
   fonte de verdade, é `frame.width / 2`. Quando a nossa fonte é mais larga
   que a do desenho, o texto passa da arte — no crachá, o nome do gerente
   subia em cima da borda. Aqui o texto é condensado (`style:text-scale`)
   até caber na largura que o CorelDRAW registrou.

2. ALTURA. A caixa emitida tem altura igual à caixa de linha da fonte
   (ascendente + descendente), e o seu TOPO é a altura de MAIÚSCULA do texto
   — não o topo da ascendente. Confirmado nos quatro textos do crachá com um
   único deslocamento global de miniatura. O LibreOffice, porém, apoia a
   primeira linha pela ascendente, o que joga o texto
   `(ascendente − maiúscula) × corpo` para baixo: 1,7 mm no título em
   Impact 22,5 pt, o bastante para o título encostar no slogan. Aqui a
   moldura sobe essa diferença.

Só mexe em moldura de texto de UMA linha cuja altura casa com a caixa de
linha da fonte (tolerância de 5%). Esse casamento é também a prova de que
identificamos a mesma fonte que o LibreOffice vai usar: se não casar, não
mexe. Texto de parágrafo (várias linhas, caixa desenhada pelo usuário) não
passa nesse teste e fica intocado.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from functools import lru_cache
from pathlib import Path

from defusedxml.ElementTree import fromstring as safe_fromstring

NS = {
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
}
FRAME = f"{{{NS['draw']}}}frame"
TEXT_BOX = f"{{{NS['draw']}}}text-box"
PARA = f"{{{NS['text']}}}p"
SPAN = f"{{{NS['text']}}}span"
STYLE = f"{{{NS['style']}}}style"
TEXT_PROPS = f"{{{NS['style']}}}text-properties"
STYLE_NAME = f"{{{NS['style']}}}name"
SPAN_STYLE = f"{{{NS['text']}}}style-name"
FONT_NAME = f"{{{NS['style']}}}font-name"
FONT_SIZE = f"{{{NS['fo']}}}font-size"
TEXT_SCALE = f"{{{NS['style']}}}text-scale"
SVG_X, SVG_Y = f"{{{NS['svg']}}}x", f"{{{NS['svg']}}}y"
SVG_W, SVG_H = f"{{{NS['svg']}}}width", f"{{{NS['svg']}}}height"

PT_TO_CM = 2.54 / 72
HEIGHT_TOL = 0.05     # a altura da moldura tem de casar com a caixa de linha
WIDTH_SANITY = 0.30   # largura medida x registrada: além disso, não é artístico
MIN_SCALE = 0.75      # nunca condensar mais que isto
SCALE_TRIGGER = 1.01  # só condensa se passar 1% da largura registrada

# Fontes ausentes -> substituta, espelhando o fonts.conf do app.
FONT_ALIASES = {
    "arialmt": "Arial",
    "arial unicode ms": "Arial",
    "helvetica": "Arial",
    "ebrima": "Segoe UI",
    "batang": "DejaVu Sans",
    "timesnewromanpsmt": "Times New Roman",
    "couriernewpsmt": "Courier New",
    "calibri": "Carlito",
    "tahoma": "DejaVu Sans",
}
FALLBACKS = ("Liberation Sans", "DejaVu Sans", "Arial")


class FontMetrics:
    """ascendente, descendente, maiúscula (fração do em) e largura do texto."""

    def __init__(self, path: Path):
        from fontTools.ttLib import TTFont

        self._font = TTFont(str(path), lazy=True, fontNumber=0)
        upm = self._font["head"].unitsPerEm or 1000
        hhea = self._font["hhea"]
        os2 = self._font["OS/2"] if "OS/2" in self._font else None
        self.upm = upm
        self.ascent = hhea.ascent / upm
        self.descent = -hhea.descent / upm
        cap = getattr(os2, "sCapHeight", 0) if os2 else 0
        if not cap:
            cap = round(0.72 * upm)  # fontes antigas sem sCapHeight
        self.cap_height = cap / upm
        self._cmap = self._font.getBestCmap()
        self._hmtx = self._font["hmtx"]

    @property
    def line_height(self) -> float:
        return self.ascent + self.descent

    def advance_em(self, text: str):
        """largura do texto em em; None se faltar glifo."""
        total = 0
        for ch in text:
            gid = self._cmap.get(ord(ch))
            if gid is None:
                return None
            total += self._hmtx[gid][0]
        return total / self.upm


@lru_cache(maxsize=1)
def _font_index(fonts_dir: str):
    """família (minúscula) -> caminho do arquivo, varrendo a pasta de fontes."""
    from fontTools.ttLib import TTFont

    index: dict[str, Path] = {}
    base = Path(fonts_dir)
    if not base.is_dir():
        return index
    for path in sorted(base.iterdir()):
        if path.suffix.lower() not in (".ttf", ".otf"):
            continue
        try:
            f = TTFont(str(path), lazy=True, fontNumber=0)
            family = f["name"].getDebugName(1)
            subfamily = (f["name"].getDebugName(2) or "").lower()
            f.close()
        except Exception:
            continue
        if not family:
            continue
        # só a face regular representa a família
        if subfamily in ("regular", "book", "") and family.lower() not in index:
            index[family.lower()] = path
        index.setdefault(f"{family} {subfamily}".strip().lower(), path)
    return index


@lru_cache(maxsize=256)
def _metrics_for(font_name: str, fonts_dir: str):
    index = _font_index(fonts_dir)
    if not index:
        return None
    names = [font_name]
    alias = FONT_ALIASES.get(font_name.strip().lower())
    if alias:
        names.append(alias)
    names.extend(FALLBACKS)
    for name in names:
        path = index.get(name.strip().lower())
        if path:
            try:
                return FontMetrics(path)
            except Exception:
                continue
    return None


_LEN = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(cm|mm|in|pt|pc|px)?\s*$")
_UNITS_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54, "pt": PT_TO_CM, "pc": 2.54 / 6, "px": 2.54 / 96}


def _cm(value):
    m = _LEN.match(value or "")
    if not m:
        return None
    return float(m.group(1)) * _UNITS_CM.get(m.group(2) or "cm", 1.0)


def _pt(value):
    cm = _cm(value)
    return None if cm is None else cm / PT_TO_CM


def _register_namespaces(xml_bytes: bytes) -> None:
    for prefix, uri in re.findall(rb'xmlns:([A-Za-z0-9_.-]+)="([^"]+)"', xml_bytes[:8000]):
        ET.register_namespace(prefix.decode(), uri.decode())


def _text_styles(root):
    out = {}
    for st in root.iter(STYLE):
        props = st.find("style:text-properties", NS)
        if props is None:
            continue
        name = st.get(STYLE_NAME)
        font = props.get(FONT_NAME)
        size = _pt(props.get(FONT_SIZE))
        if name and font and size:
            out[name] = (font, size, props)
    return out


def fix_text_boxes(odg_path: Path, fonts_dir: Path) -> dict:
    """Reescreve o ODG no lugar. Devolve contagem do que foi corrigido."""
    with zipfile.ZipFile(odg_path) as zin:
        entries = {n: zin.read(n) for n in zin.namelist()}
    if "content.xml" not in entries:
        return {"moved": 0, "condensed": 0}

    _register_namespaces(entries["content.xml"])
    root = safe_fromstring(entries["content.xml"])
    styles = _text_styles(root)
    moved = condensed = 0

    for frame in root.iter(FRAME):
        box = frame.find("draw:text-box", NS)
        if box is None:
            continue
        paras = box.findall("text:p", NS)
        if len(paras) != 1:
            continue
        spans = paras[0].findall("text:span", NS)
        if not spans:
            continue
        text = "".join(s.text or "" for s in spans)
        if not text.strip():
            continue
        style_name = spans[0].get(SPAN_STYLE)
        if style_name not in styles:
            continue
        font_name, size_pt, _props = styles[style_name]
        h = _cm(frame.get(SVG_H))
        w = _cm(frame.get(SVG_W))
        y = _cm(frame.get(SVG_Y))
        if None in (h, w, y) or h <= 0 or w <= 0:
            continue

        fm = _metrics_for(font_name, str(fonts_dir))
        if fm is None:
            continue
        line_cm = size_pt * fm.line_height * PT_TO_CM
        if line_cm <= 0 or abs(h - line_cm) / h > HEIGHT_TOL:
            continue  # não é uma linha de texto artístico, ou a fonte não confere

        # --- vertical: o topo da caixa é a altura de maiúscula ---------------
        shift = size_pt * (fm.ascent - fm.cap_height) * PT_TO_CM
        if shift > 0.005:
            frame.set(SVG_Y, f"{y - shift:.4f}cm")
            moved += 1

        # --- horizontal: caber na largura que o CorelDRAW registrou ----------
        adv_em = fm.advance_em(text)
        if adv_em is None:
            continue
        advance = adv_em * size_pt * PT_TO_CM
        target = w / 2.0  # a libcdr dobra a largura do texto artístico
        if advance <= 0 or target <= 0:
            continue
        if abs(advance - target) / target > WIDTH_SANITY:
            continue  # a regra do dobro não vale aqui: não mexe
        if advance > target * SCALE_TRIGGER:
            scale = max(MIN_SCALE, target / advance)
            for span in spans:
                sname = span.get(SPAN_STYLE)
                entry = styles.get(sname)
                if entry:
                    entry[2].set(TEXT_SCALE, f"{scale * 100:.1f}%")
            condensed += 1

    if not moved and not condensed:
        return {"moved": 0, "condensed": 0}

    entries["content.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        if "mimetype" in entries:  # o ODF exige mimetype primeiro e sem compressão
            zout.writestr("mimetype", entries.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
        for name, data in entries.items():
            zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    odg_path.write_bytes(buf.getvalue())
    return {"moved": moved, "condensed": condensed}
