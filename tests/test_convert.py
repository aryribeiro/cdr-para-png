"""Testes do conversor CDR -> PNG.

Rodam fora do Streamlit (modo "bare"): os st.* viram avisos inofensivos.
A conversão de ponta a ponta precisa do LibreOffice (soffice) instalado.
"""
import io
import shutil
import struct
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
OUTPUT = Path(__file__).parent / "output"
HAS_SOFFICE = shutil.which("soffice") is not None or shutil.which("libreoffice") is not None


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "não é PNG"
    return struct.unpack(">II", data[16:24])


# --- algoritmo de versão (igual ao da libcdr) -------------------------------

def test_cdr_version_algoritmo():
    def riff(tag: bytes) -> bytes:
        return b"RIFF" + b"\x00\x00\x00\x00" + tag + b"\x00" * 8
    assert app.cdr_version(riff(b"CDR7")) == 700
    assert app.cdr_version(riff(b"cdr8")) == 800
    assert app.cdr_version(riff(b"CDR ")) == 300
    assert app.cdr_version(riff(b"CDRA")) == 1000   # CorelDRAW 10, não X3
    assert app.cdr_version(riff(b"CDRD")) == 1300   # X3
    assert app.cdr_version(riff(b"CDRE")) == 1400   # X4
    assert app.cdr_version(riff(b"CDR0")) == 0
    assert app.cdr_version(riff(b"WAVE")) == 0
    assert app.cdr_version(b"WL" + b"\x00" * 14) == 200
    assert app.version_name(1300) == "X3"
    assert app.version_name(700) == "7"


# --- inspeção do cabeçalho -------------------------------------------------

def test_header_fixtures():
    assert app.inspect_header((FIXTURES / "corel_arrows_x3.cdr").read_bytes()) == ("cdr", "X3")
    assert app.inspect_header((FIXTURES / "text_rgb_fill_cdr7.cdr").read_bytes()) == ("cdr", "7")
    assert app.inspect_header((FIXTURES / "fdo48739-1.cdr").read_bytes()) == ("cdr", "X4 ou posterior")
    assert app.inspect_header((FIXTURES / "shapes_v1.cdr").read_bytes())[0] == "cdr"


def test_header_impostores():
    assert app.inspect_header(b"%PDF-1.7 lixo" + b"\x00" * 16)[1] == "PDF"
    assert app.inspect_header(b'<?xml version="1.0"?><svg xmlns="x"></svg>')[1] == "SVG"
    assert app.inspect_header(b'<?xml version="1.0"?><root/>' + b" " * 16)[1] == "XML"
    assert app.inspect_header(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')[1] == "SVG"
    assert app.inspect_header(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)[1] == "PNG"
    assert app.inspect_header(b"")[0] == "outro"
    # ODG renomeado para .cdr: ZIP com mimetype + content.xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.graphics")
        z.writestr("content.xml", "<x/>")
    assert app.inspect_header(buf.getvalue()) == ("outro", "OpenDocument renomeado")
    # ZIP qualquer
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "x")
    assert app.inspect_header(buf.getvalue()) == ("outro", "ZIP")


# --- conversão de ponta a ponta -------------------------------------------

@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice (soffice) ausente")
@pytest.mark.parametrize("name,version", [
    ("corel_arrows_x3.cdr", "X3"),
    ("text_rgb_fill_cdr7.cdr", "7"),
    ("fdo48739-1.cdr", "X4 ou posterior"),
    ("shapes_v1.cdr", None),
])
def test_cdr_para_png(name, version):
    OUTPUT.mkdir(exist_ok=True)
    png, info = app.convert_cdr_to_png(str(FIXTURES / name))
    width, height = png_size(png)
    assert max(width, height) == app.LONG_SIDE_PX
    assert info["pages"] >= 1
    assert info["width_cm"] > 0 and info["height_cm"] > 0
    if version:
        assert info["version"] == version
    (OUTPUT / (Path(name).stem + ".png")).write_bytes(png)


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice (soffice) ausente")
def test_poster_x4_nao_sai_em_branco():
    """O pôster do X4 (bug fdo48739) é todo vetorial (texto em curvas); a
    prova de que a libcdr trouxe o conteúdo é a imagem ter tinta de verdade."""
    import pymupdf
    png, _info = app.convert_cdr_to_png(str(FIXTURES / "fdo48739-1.cdr"))
    pix = pymupdf.Pixmap(png)
    samples = pix.samples
    non_white = sum(1 for b in samples[::97] if b < 240)
    assert non_white / (len(samples) // 97) > 0.2


def test_svg_renomeado_e_recusado_antes_do_libreoffice(tmp_path):
    fake = tmp_path / "logo.cdr"
    fake.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>')
    with pytest.raises(app.ConversionError, match="SVG"):
        app.convert_cdr_to_png(str(fake))


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice (soffice) ausente")
def test_cdr_corrompido(tmp_path):
    data = bytearray((FIXTURES / "corel_arrows_x3.cdr").read_bytes())
    data[64:] = b"\x00" * (len(data) - 64)
    fake = tmp_path / "corrompido.cdr"
    fake.write_bytes(bytes(data))
    with pytest.raises(app.ConversionError):
        app.convert_cdr_to_png(str(fake))
