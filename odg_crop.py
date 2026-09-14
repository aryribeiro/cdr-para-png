"""Reconstrói recortes de imagem que a libcdr perde ao ler CDR.

Medido (14/09/2026, CorelDRAW X5, LibreOffice 26.2 / libcdr): para cada
bitmap recortado no Corel, a libcdr grava no ODG um `draw:polygon` invisível
(sem traço e sem preenchimento) com o retângulo do recorte, seguido de um
`draw:frame` com a imagem INTEIRA, sem clip. A foto aparece completa e cobre
o resto do desenho. O código-fonte da libcdr não tem nenhuma noção de clip.

Aqui: quando um frame de imagem vem logo depois de um polígono invisível
menor que ele e contido nele, os pixels da imagem são cortados na proporção
do polígono e o frame passa a ocupar o retângulo do polígono. Recortes não
retangulares (elipse, forma livre) viram o retângulo envolvente — melhor do
que a imagem inteira, mas não exato.
"""
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pymupdf
from defusedxml.ElementTree import fromstring as safe_fromstring

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}
_UNITS_TO_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54, "pt": 2.54 / 72, "pc": 2.54 / 6, "px": 2.54 / 96}
_LEN = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(cm|mm|in|pt|pc|px)?\s*$")

SHAPE_TAGS = {f"{{{NS['draw']}}}{t}" for t in ("polygon", "path", "rect", "custom-shape", "polyline")}
FRAME_TAG = f"{{{NS['draw']}}}frame"
IMAGE_TAG = f"{{{NS['draw']}}}image"
TRANSFORM = f"{{{NS['draw']}}}transform"
STYLE_NAME = f"{{{NS['draw']}}}style-name"
HREF = f"{{{NS['xlink']}}}href"
MIN_SHRINK = 0.98   # o polígono precisa ser menor que o frame (área) para ser recorte


def _register_namespaces(xml_bytes: bytes) -> None:
    for prefix, uri in re.findall(rb'xmlns:([A-Za-z0-9_.-]+)="([^"]+)"', xml_bytes[:8000]):
        ET.register_namespace(prefix.decode(), uri.decode())


def _cm(value: str):
    m = _LEN.match(value or "")
    if not m:
        return None
    return float(m.group(1)) * _UNITS_TO_CM.get(m.group(2) or "cm", 1.0)


def _bbox(el):
    x = _cm(el.get(f"{{{NS['svg']}}}x"))
    y = _cm(el.get(f"{{{NS['svg']}}}y"))
    w = _cm(el.get(f"{{{NS['svg']}}}width"))
    h = _cm(el.get(f"{{{NS['svg']}}}height"))
    if None in (x, y, w, h) or w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _set_bbox(el, bbox):
    x, y, w, h = bbox
    el.set(f"{{{NS['svg']}}}x", f"{x:.4f}cm")
    el.set(f"{{{NS['svg']}}}y", f"{y:.4f}cm")
    el.set(f"{{{NS['svg']}}}width", f"{w:.4f}cm")
    el.set(f"{{{NS['svg']}}}height", f"{h:.4f}cm")


def _invisible_styles(root) -> set:
    """Nomes dos estilos automáticos sem traço e sem preenchimento."""
    names = set()
    for st in root.iter(f"{{{NS['style']}}}style"):
        gp = st.find("style:graphic-properties", NS)
        if gp is None:
            continue
        stroke = gp.get(f"{{{NS['draw']}}}stroke")
        fill = gp.get(f"{{{NS['draw']}}}fill")
        if stroke == "none" and fill == "none":
            names.add(st.get(f"{{{NS['style']}}}name"))
    return names


def _crop_png(image_bytes: bytes, frac) -> bytes:
    """Corta a imagem pela fração (l, t, r, b) do seu retângulo, pixel a
    pixel, sem reamostrar (abrir como "documento" e renderizar perderia a
    resolução nativa: a página sairia em pontos a 72 dpi)."""
    src = pymupdf.Pixmap(image_bytes)
    if src.alpha:
        src = pymupdf.Pixmap(src, 0)  # descarta alfa: o PNG final é opaco
    if src.n - src.alpha != 3:
        src = pymupdf.Pixmap(pymupdf.csRGB, src)
    w, h = src.width, src.height
    l, t, r, b = frac
    irect = pymupdf.IRect(round(w * l), round(h * t), round(w * r), round(h * b))
    if irect.is_empty or irect.width < 1 or irect.height < 1:
        raise ValueError("recorte vazio")
    # Pixmap(fonte, largura, altura, clip): copia reescalada para largura x
    # altura e recorta pelo clip NESSA escala; com a escala original, o clip
    # é pixel a pixel (clip fora da escala derruba o processo).
    out = pymupdf.Pixmap(src, w, h, irect)
    return out.tobytes("png")


def restore_image_crops(odg_path: Path) -> int:
    """Reescreve o ODG no lugar. Devolve quantas imagens foram recortadas."""
    with zipfile.ZipFile(odg_path) as zin:
        entries = {n: zin.read(n) for n in zin.namelist()}
    if "content.xml" not in entries:
        return 0

    _register_namespaces(entries["content.xml"])
    root = safe_fromstring(entries["content.xml"])
    invisible = _invisible_styles(root)
    new_files = {}
    fixed = 0

    for parent in root.iter():
        children = list(parent)
        for i in range(1, len(children)):
            frame, shape = children[i], children[i - 1]
            if frame.tag != FRAME_TAG or shape.tag not in SHAPE_TAGS:
                continue
            image = frame.find("draw:image", NS)
            if image is None or image.get(HREF) is None:
                continue
            if frame.get(TRANSFORM) or shape.get(TRANSFORM):
                continue
            if shape.get(STYLE_NAME) not in invisible:
                continue
            fb, sb = _bbox(frame), _bbox(shape)
            if fb is None or sb is None:
                continue
            fx, fy, fw, fh = fb
            sx, sy, sw, sh = sb
            tol = 0.01
            inside = (sx >= fx - tol and sy >= fy - tol
                      and sx + sw <= fx + fw + tol and sy + sh <= fy + fh + tol)
            if not inside or (sw * sh) / (fw * fh) > MIN_SHRINK:
                continue
            href = image.get(HREF)
            if href not in entries:
                continue
            frac = (
                max(0.0, (sx - fx) / fw), max(0.0, (sy - fy) / fh),
                min(1.0, (sx + sw - fx) / fw), min(1.0, (sy + sh - fy) / fh),
            )
            try:
                cropped = _crop_png(entries[href], frac)
            except Exception:
                continue
            stem = Path(href).stem
            new_href = f"Pictures/{stem}_crop{fixed + 1}.png"
            new_files[new_href] = cropped
            image.set(HREF, new_href)
            _set_bbox(frame, sb)
            fixed += 1

    if not fixed:
        return 0

    entries["content.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    entries.update(new_files)

    manifest_name = "META-INF/manifest.xml"
    if manifest_name in entries:
        _register_namespaces(entries[manifest_name])
        mroot = safe_fromstring(entries[manifest_name])
        for new_href in new_files:
            ET.SubElement(mroot, f"{{{NS['manifest']}}}file-entry", {
                f"{{{NS['manifest']}}}full-path": new_href,
                f"{{{NS['manifest']}}}media-type": "image/png",
            })
        entries[manifest_name] = ET.tostring(mroot, encoding="utf-8", xml_declaration=True)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        if "mimetype" in entries:  # o ODF exige mimetype primeiro e sem compressão
            zout.writestr("mimetype", entries.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
        for name, data in entries.items():
            zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    odg_path.write_bytes(buf.getvalue())
    return fixed
