"""Testes do odg_text: posição e largura do texto artístico."""
import io
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import odg_text  # noqa: E402

FONTS = ROOT / "static" / "fonts"
PT_TO_CM = 2.54 / 72

CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" office:version="1.3">
 <office:automatic-styles>
  <style:style style:name="T1" style:family="text"><style:text-properties style:font-name="{font}" fo:font-size="{size}pt"/></style:style>
 </office:automatic-styles>
 <office:body><office:drawing><draw:page draw:name="page1">
  <draw:frame draw:style-name="gr1" svg:x="{x}cm" svg:y="{y}cm" svg:width="{w}cm" svg:height="{h}cm">
   <draw:text-box>{paras}</draw:text-box>
  </draw:frame>
 </draw:page></office:drawing></office:body>
</office:document-content>
"""
PARA = '<text:p><text:span text:style-name="T1">{t}</text:span></text:p>'


def metrics(font="Arial"):
    fm = odg_text._metrics_for(font, str(FONTS))
    assert fm is not None, f"fonte {font} não encontrada em static/fonts"
    return fm


def make_odg(path, text="Rodrigo Denicolo", font="Arial", size=19.5,
             x=1.0, y=5.0, w=None, h=None, paras=1):
    fm = metrics(font)
    if h is None:
        h = size * fm.line_height * PT_TO_CM          # caixa de UMA linha
    if w is None:
        w = 2 * fm.advance_em(text) * size * PT_TO_CM  # a libcdr dobra
    body = "".join(PARA.format(t=text) for _ in range(paras))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.graphics",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", CONTENT.format(font=font, size=size, x=x, y=y, w=w, h=h, paras=body))
    path.write_bytes(buf.getvalue())
    return {"w": w, "h": h, "fm": fm}


def read(path):
    with zipfile.ZipFile(path) as z:
        return z.read("content.xml").decode("utf-8")


def frame_attrs(content):
    m = re.search(r"<draw:frame([^>]*)>", content)
    return dict(re.findall(r'(svg:x|svg:y|svg:width|svg:height)="([^"]*)"', m.group(1)))


def test_sobe_a_moldura_pela_altura_de_maiuscula(tmp_path):
    """O topo da caixa da libcdr é a altura de maiúscula; o LibreOffice apoia
    o texto pela ascendente. A moldura tem de subir a diferença."""
    odg = tmp_path / "a.odg"
    info = make_odg(odg, y=5.0, size=19.5)
    res = odg_text.fix_text_boxes(odg, FONTS)
    assert res["moved"] == 1
    fm = info["fm"]
    esperado = 5.0 - 19.5 * (fm.ascent - fm.cap_height) * PT_TO_CM
    y = float(frame_attrs(read(odg))["svg:y"].replace("cm", ""))
    assert y == pytest.approx(esperado, abs=0.001)
    assert y < 5.0  # subiu


def test_condensa_texto_mais_largo_que_a_caixa(tmp_path):
    """Se a nossa fonte é mais larga que a do desenho, o texto passa da arte:
    condensa até a largura que o CorelDRAW registrou (metade da emitida)."""
    odg = tmp_path / "b.odg"
    fm = metrics("Arial")
    largura_real = fm.advance_em("Rodrigo Denicolo") * 19.5 * PT_TO_CM
    # o CorelDRAW registrou 10% menos que a nossa fonte ocupa
    make_odg(odg, w=2 * largura_real * 0.90)
    res = odg_text.fix_text_boxes(odg, FONTS)
    assert res["condensed"] == 1
    m = re.search(r'style:text-scale="([\d.]+)%"', read(odg))
    assert m, "faltou style:text-scale"
    assert float(m.group(1)) == pytest.approx(90.0, abs=0.5)


def test_nao_condensa_quando_ja_cabe(tmp_path):
    odg = tmp_path / "c.odg"
    make_odg(odg)  # largura exatamente o dobro da nossa: já cabe
    res = odg_text.fix_text_boxes(odg, FONTS)
    assert res["condensed"] == 0
    assert "text-scale" not in read(odg)


def test_nao_condensa_alem_do_limite(tmp_path):
    odg = tmp_path / "d.odg"
    fm = metrics("Arial")
    largura_real = fm.advance_em("Rodrigo Denicolo") * 19.5 * PT_TO_CM
    make_odg(odg, w=2 * largura_real * 0.5)  # caixa absurda: regra do dobro não vale
    res = odg_text.fix_text_boxes(odg, FONTS)
    assert res["condensed"] == 0


def test_ignora_texto_de_paragrafo(tmp_path):
    """Caixa de várias linhas não é texto artístico: não se mexe."""
    odg = tmp_path / "e.odg"
    make_odg(odg, paras=3, h=3.0)
    antes = frame_attrs(read(odg))
    res = odg_text.fix_text_boxes(odg, FONTS)
    assert res == {"moved": 0, "condensed": 0}
    assert frame_attrs(read(odg)) == antes


def test_ignora_altura_que_nao_e_caixa_de_linha(tmp_path):
    """Se a altura não casa com a caixa de linha da fonte, não identificamos a
    fonte que o LibreOffice vai usar — então não mexe."""
    odg = tmp_path / "f.odg"
    fm = metrics("Arial")
    make_odg(odg, h=19.5 * fm.line_height * PT_TO_CM * 1.4)
    assert odg_text.fix_text_boxes(odg, FONTS) == {"moved": 0, "condensed": 0}


def test_mimetype_continua_primeiro_e_sem_compressao(tmp_path):
    odg = tmp_path / "g.odg"
    make_odg(odg)
    odg_text.fix_text_boxes(odg, FONTS)
    with zipfile.ZipFile(odg) as z:
        assert z.namelist()[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
