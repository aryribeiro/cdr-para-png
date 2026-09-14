"""Testes do odg_crop: reconstrução do recorte de imagem perdido pela libcdr."""
import io
import struct
import sys
import zipfile
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import odg_crop  # noqa: E402

PRIVATE = Path(__file__).parent / "fixtures" / "private"

CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink" office:version="1.3">
 <office:automatic-styles>
  <style:style style:name="gr1" style:family="graphic"><style:graphic-properties draw:stroke="none" draw:fill="none"/></style:style>
  <style:style style:name="gr2" style:family="graphic"><style:graphic-properties draw:stroke="solid" draw:fill="none"/></style:style>
 </office:automatic-styles>
 <office:body><office:drawing><draw:page draw:name="page1">
  <draw:polygon draw:style-name="{shape_style}" svg:x="{sx}cm" svg:y="{sy}cm" svg:width="{sw}cm" svg:height="{sh}cm" draw:points="0,0 1,0 1,1 0,1"/>
  <draw:frame draw:style-name="gr1" svg:x="0cm" svg:y="0cm" svg:width="10cm" svg:height="20cm">
   <draw:image xlink:href="Pictures/foto.png"/>
  </draw:frame>
 </draw:page></office:drawing></office:body>
</office:document-content>
"""
MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.graphics"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="Pictures/foto.png" manifest:media-type="image/png"/>
</manifest:manifest>
"""


def make_png(width, height):
    """PNG com metade esquerda vermelha e direita azul, para checar o corte."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    for x in range(width):
        for y in range(height):
            pix.set_pixel(x, y, (255, 0, 0) if x < width // 2 else (0, 0, 255))
    return pix.tobytes("png")


def make_odg(path, shape_style="gr1", sx=2, sy=5, sw=4, sh=5):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.graphics", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", CONTENT.format(shape_style=shape_style, sx=sx, sy=sy, sw=sw, sh=sh))
        z.writestr("META-INF/manifest.xml", MANIFEST)
        z.writestr("Pictures/foto.png", make_png(100, 200))
    path.write_bytes(buf.getvalue())


def png_size(data):
    return struct.unpack(">II", data[16:24])


def test_recorte_e_reaplicado(tmp_path):
    odg = tmp_path / "a.odg"
    make_odg(odg)  # polígono 4x5 cm em (2,5) dentro do frame 10x20 cm
    assert odg_crop.restore_image_crops(odg) == 1
    with zipfile.ZipFile(odg) as z:
        names = z.namelist()
        assert names[0] == "mimetype" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        content = z.read("content.xml").decode("utf-8")
        manifest = z.read("META-INF/manifest.xml").decode("utf-8")
        new = [n for n in names if n.startswith("Pictures/foto_crop")]
        assert len(new) == 1
        cropped = z.read(new[0])
    # frame passou a ocupar o retângulo do polígono
    assert 'svg:x="2.0000cm"' in content and 'svg:width="4.0000cm"' in content
    assert 'svg:height="5.0000cm"' in content
    assert new[0] in content and new[0] in manifest
    # imagem 100x200 cortada em 20%..60% da largura e 25%..50% da altura -> 40x50
    assert png_size(cropped) == (40, 50)
    # o corte pega a fronteira vermelho/azul (x=50 fica dentro de 20..60)
    pix = pymupdf.Pixmap(cropped)
    assert pix.pixel(0, 0)[:3] == (255, 0, 0) and pix.pixel(39, 0)[:3] == (0, 0, 255)


def test_poligono_visivel_nao_e_recorte(tmp_path):
    odg = tmp_path / "b.odg"
    make_odg(odg, shape_style="gr2")  # tem traço: é uma forma de verdade
    assert odg_crop.restore_image_crops(odg) == 0


def test_poligono_do_mesmo_tamanho_nao_e_recorte(tmp_path):
    odg = tmp_path / "c.odg"
    make_odg(odg, sx=0, sy=0, sw=10, sh=20)
    assert odg_crop.restore_image_crops(odg) == 0


def test_poligono_fora_do_frame_nao_e_recorte(tmp_path):
    odg = tmp_path / "d.odg"
    make_odg(odg, sx=8, sy=5, sw=4, sh=5)  # sai pela direita
    assert odg_crop.restore_image_crops(odg) == 0


@pytest.mark.skipif(not (PRIVATE / "cracha.cdr").exists(), reason="fixture privada ausente")
def test_cracha_real_recupera_as_fotos():
    """Crachá real do dono (dados pessoais: fica fora do git). Antes do
    conserto, as fotos inteiras cobriam nome e telefone."""
    import app
    png, info = app.convert_cdr_to_png(str(PRIVATE / "cracha.cdr"))
    assert info["cropped_images"] == 2
    # o título usa Impact; com static/fonts o LibreOffice embute a Impact de
    # verdade em vez de substituir por uma fonte larga que estoura a arte
    assert any("Impact" in f for f in info["fonts"]), info["fonts"]
    # os dois nomes existem; a libcdr do Debian bookworm (LibreOffice 7.4)
    # descartava o parágrafo com dois idiomas — em trixie e no Cloud não
    assert "Rodrigo Denicolo" in info["text"] and "Carlos Eduardo" in info["text"], info["text"]
    pix = pymupdf.Pixmap(png)
    # a imagem é da arte (dois crachás lado a lado, mais larga que alta),
    # não da página A4 em pé
    assert info["cropped_to_art"] and info["page_height_cm"] > 29
    assert pix.width > pix.height, (pix.width, pix.height)
    assert info["width_cm"] < 15 and info["height_cm"] < 12
    # o nome fica visível de novo: a imagem final precisa ter tinta escura em
    # alguma linha da faixa do nome (entre 65% e 80% da altura da arte)
    found = False
    for y in range(int(pix.height * 0.65), int(pix.height * 0.80), 8):
        dark = sum(1 for x in range(int(pix.width * 0.02), int(pix.width * 0.45), 4) if sum(pix.pixel(x, y)[:3]) < 200)
        if dark > 5:
            found = True
            break
    assert found
