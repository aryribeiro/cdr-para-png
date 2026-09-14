import streamlit as st
import subprocess
import io
import os
import re
import tempfile
import shutil
import uuid
import zipfile
from pathlib import Path
import platform
import threading
import time
import random
from xml.sax.saxutils import escape

import pymupdf

from odg_crop import restore_image_crops

# ---------------------------------------------------------------------------
# SETUP DO PATH DO LIBREOFFICE
# ---------------------------------------------------------------------------
def setup_libreoffice_path():
    """Adiciona o LibreOffice ao PATH do sistema se necessário."""
    if platform.system() == "Windows":
        possible_paths = [
            r"C:\Program Files\LibreOffice\program",
            r"C:\Program Files (x86)\LibreOffice\program",
        ]
    elif platform.system() == "Darwin":
        possible_paths = ["/Applications/LibreOffice.app/Contents/MacOS"]
    else:
        possible_paths = ["/usr/bin", "/usr/local/bin"]

    for path in possible_paths:
        if os.path.exists(path) and path not in os.environ.get("PATH", ""):
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            break

setup_libreoffice_path()

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO DE PÁGINA E CSS (compacto, para caber no lightbox do AtlasDocs)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Conversor CDR para PNG",
    page_icon="🎨",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .main {
        background-color: #ffffff;
        color: #333333;
    }
    .block-container {
        padding-top: 0rem !important;
        padding-bottom: 0rem !important;
        max-width: 28rem !important;
    }
    header {display: none !important;}
    footer {display: none !important;}
    #MainMenu {display: none !important;}
    div[data-testid="stAppViewBlockContainer"] {
        padding-top: 0 !important;
        padding-bottom: 0 !important;
    }
    div[data-testid="stVerticalBlock"] {
        gap: 0 !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
    }
    .element-container {
        margin-top: 0 !important;
        margin-bottom: 0 !important;
    }
    .stDownloadButton button {
        width: 100% !important;
        padding: 0.6rem 2rem;
    }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# CONVERSÃO: CDR (CorelDRAW) -> PNG
# Escopo único e fixo — este app não lida com nenhum outro par de formatos.
# Cadeia: LibreOffice Draw (libcdr) -> ODG -> recortes de imagem restaurados
# (odg_crop) -> LibreOffice -> PDF -> PyMuPDF -> PNG da 1ª página.
# ---------------------------------------------------------------------------
SOURCE_EXT = ".cdr"
TARGET_EXT = "png"
TARGET_MIME = "image/png"

LONG_SIDE_PX = 4000        # lado maior da imagem, em pixels
SOFFICE_TIMEOUT = 180      # segundos por tentativa
ART_MARGIN = 0.03          # folga em volta da arte (fração do lado maior da arte)
ART_MIN_PT = 20            # arte menor que isto (em pontos) = usar a página inteira


class ConversionError(RuntimeError):
    """Falha de conversão — o erro já foi exibido na interface."""


# ---------------------------------------------------------------------------
# INSPEÇÃO DO CABEÇALHO: versão do CorelDRAW e impostores
# O LibreOffice fareja o conteúdo e ignora a extensão: um SVG ou PDF
# renomeado para .cdr "converte com sucesso" sem nunca tocar a libcdr, e sai
# carregado como Impress. Por isso a entrada é conferida antes.
# ---------------------------------------------------------------------------
def cdr_version(data: bytes) -> int:
    """Algoritmo de versão exatamente como a libcdr faz (getCDRVersion em
    CDRDocument.cpp). 'CDRA' é o CorelDRAW 10, não o X3 (que é 'CDRD')."""
    if len(data) < 12:
        return 0
    if data[0:2] == b"WL":
        return 200  # formato anterior a 1992
    if data[0:4] != b"RIFF":
        return 0
    if data[8:11].upper() != b"CDR":
        return 0
    c = data[11]
    if c == 0x20:
        return 300
    if c < 0x31:
        return 0
    if c < 0x3A:
        return 100 * (c - 0x30)      # '1'..'9' -> 100..900
    if c < 0x41:
        return 0
    return 100 * (c - 0x37)          # 'A' -> 1000, 'D' -> 1300 (X3), 'E' -> 1400 (X4)


_VERSION_NAMES = {1000: "10", 1100: "11", 1200: "12", 1300: "X3", 1400: "X4",
                  1500: "X5", 1600: "X6", 1700: "X7"}


def version_name(v: int) -> str:
    if v == 200:
        return "anterior a 1992"
    if v <= 900:
        return str(v // 100)
    return _VERSION_NAMES.get(v, f"versão interna {v}")


def sniff_impostor(data: bytes):
    head = data[:512]
    if head.startswith(b"%PDF-"):
        return "PDF"
    if head.startswith(b"%!PS"):
        return "PostScript/EPS"
    if re.search(rb"<svg[\s>]", head):
        return "SVG"
    if re.match(rb"\s*<\?xml", head):
        return "XML"
    if head.startswith(b"\x89PNG"):
        return "PNG"
    if head.startswith(b"\xff\xd8"):
        return "JPEG"
    if head.startswith(b"\xd0\xcf"):
        return "documento OLE"
    return None


def inspect_header(data: bytes):
    """Devolve (tipo, rótulo). tipo: 'cdr' ou 'outro'."""
    if len(data) < 16:
        return "outro", "arquivo vazio"
    if data[:2] == b"PK":
        try:
            names = [n.lower() for n in zipfile.ZipFile(io.BytesIO(data)).namelist()]
        except zipfile.BadZipFile:
            return "outro", "ZIP corrompido"
        if "mimetype" in names and "content.xml" in names:
            return "outro", "OpenDocument renomeado"
        if any(n.endswith("riffdata.cdr") or n.endswith("root.dat") for n in names):
            return "cdr", "X4 ou posterior"
        return "outro", "ZIP"
    imp = sniff_impostor(data)
    if imp:
        return "outro", imp
    v = cdr_version(data)
    if v == 0:
        return "outro", "desconhecido"
    return "cdr", version_name(v)


# ---------------------------------------------------------------------------
# FONTES: o CDR referencia fontes pelo nome (Impact, Arial, Wingdings...).
# Sem elas no servidor, o LibreOffice substitui por outra de largura
# diferente e o texto estoura a arte. static/fonts leva a mesma coleção do
# app libreoffice; o fontconfig e o SAL_VCL_FONTPATH fazem o LibreOffice
# enxergá-la. Roda 1x a cada 24h (cache_resource com TTL).
# ---------------------------------------------------------------------------
STATIC_FONTS_DIR = Path(__file__).resolve().parent / "static" / "fonts"
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc"}


@st.cache_resource(ttl=86400, show_spinner=False)
def prepare_font_environment():
    """Copia as fontes do repo para as pastas de fonte do usuário, gera um
    fonts.conf com aliases e devolve o ambiente para o soffice."""
    user_fonts = Path.home() / ".fonts"
    user_share_fonts = Path.home() / ".local" / "share" / "fonts"
    for d in [user_fonts, user_share_fonts]:
        d.mkdir(parents=True, exist_ok=True)

    if STATIC_FONTS_DIR.is_dir():
        for font_path in STATIC_FONTS_DIR.rglob("*"):
            if font_path.is_file() and font_path.suffix.lower() in FONT_EXTENSIONS:
                for target_dir in [user_fonts, user_share_fonts]:
                    dest = target_dir / font_path.name
                    if not dest.exists() or dest.stat().st_size != font_path.stat().st_size:
                        try:
                            shutil.copy2(font_path, dest)
                        except OSError:
                            pass

    if platform.system() == "Linux":
        try:
            subprocess.run(["fc-cache", "-f", str(STATIC_FONTS_DIR), str(user_fonts)],
                           capture_output=True, timeout=60)
        except Exception:
            pass

    config_dir = Path.home() / ".config" / "fontconfig"
    config_dir.mkdir(parents=True, exist_ok=True)
    conf_file = config_dir / "fonts.conf"
    xml_lines = [
        '<?xml version="1.0"?>',
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">',
        '<fontconfig>',
        '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>',
        f'  <dir>{escape(str(STATIC_FONTS_DIR))}</dir>',
        f'  <dir>{escape(str(user_fonts))}</dir>',
        f'  <dir>{escape(str(user_share_fonts))}</dir>',
        '  <!-- Aliases: nomes PostScript e fontes ausentes caem em parentes de métrica parecida -->',
    ]
    alias_maps = [
        ("ArialMT", ["Arial", "Liberation Sans", "DejaVu Sans", "sans-serif"]),
        ("Arial Unicode MS", ["Arial", "Liberation Sans", "DejaVu Sans", "sans-serif"]),
        ("Helvetica", ["Arial", "Liberation Sans", "sans-serif"]),
        ("Calibri", ["Calibri", "Carlito", "Liberation Sans", "sans-serif"]),
        ("Tahoma", ["Tahoma", "DejaVu Sans", "sans-serif"]),
        ("Segoe UI", ["Segoe UI", "DejaVu Sans", "sans-serif"]),
        ("Ebrima", ["Segoe UI", "DejaVu Sans", "sans-serif"]),
        ("TimesNewRomanPSMT", ["Times New Roman", "Liberation Serif", "serif"]),
        ("CourierNewPSMT", ["Courier New", "Liberation Mono", "monospace"]),
    ]
    for source_font, target_list in alias_maps:
        xml_lines.append('  <alias>')
        xml_lines.append(f'    <family>{escape(source_font)}</family>')
        xml_lines.append('    <prefer>')
        for tgt in target_list:
            xml_lines.append(f'      <family>{escape(tgt)}</family>')
        xml_lines.append('    </prefer>')
        xml_lines.append('  </alias>')
    xml_lines.append('</fontconfig>')
    conf_file.write_text("\n".join(xml_lines), encoding="utf-8")

    env = os.environ.copy()
    env["FONTCONFIG_FILE"] = str(conf_file)
    env["FONTCONFIG_PATH"] = str(config_dir)
    env["SAL_VCL_FONTPATH"] = os.pathsep.join(str(p) for p in (STATIC_FONTS_DIR, user_fonts, user_share_fonts))
    return env


# ---------------------------------------------------------------------------
# RESILIÊNCIA: LIMITE DE PROCESSOS CONCORRENTES E LIMPEZA DE ÓRFÃOS
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_conversion_slots():
    """Semáforo global do processo: no máximo 2 instâncias simultâneas do
    LibreOffice. Cada soffice consome ~200-300 MB; sem este limite, N
    usuários simultâneos = N processos e o container do Streamlit Cloud
    (1 GB) morre por falta de memória. Usuários excedentes aguardam a vez."""
    return threading.BoundedSemaphore(2)


@st.cache_resource(show_spinner=False)
def cleanup_stale_artifacts():
    """Remove perfis lo_profile_* e pastas cdr_* órfãos (de execuções que
    morreram no meio) com mais de 1h, evitando encher o disco do container.
    Roda 1x por boot."""
    cutoff = time.time() - 3600
    tmp = Path(tempfile.gettempdir())
    for pattern in ("lo_profile_*", "cdr_*"):
        for path in tmp.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink()
            except OSError:
                pass
    return True


# ---------------------------------------------------------------------------
# ETAPA 1: CDR -> PDF COM O LIBREOFFICE (backoff exponencial, perfil isolado)
# ---------------------------------------------------------------------------
def run_lo_subprocess_with_backoff(cmd_args, env, max_retries=3, base_delay=0.5, max_delay=3.0):
    result = None
    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SOFFICE_TIMEOUT,
                env=env,
            )
            if result.returncode == 0:
                return result
        except (subprocess.TimeoutExpired, OSError):
            result = None

        if attempt < max_retries - 1:
            calculated_delay = min(max_delay, base_delay * (2 ** attempt))
            jitter = random.uniform(0, 0.3)
            time.sleep(calculated_delay + jitter)

    return result


_LOADED_AS = re.compile(r"as a (\w+) document")


def run_soffice_convert(input_path: Path, output_dir: Path, filter_name: str, ext: str) -> Path:
    """Roda `soffice --convert-to` com perfil de usuário próprio por execução
    (sem isso, conversões simultâneas falham caladas). Devolve o caminho do
    arquivo gerado ou levanta ConversionError com a mensagem já exibida."""
    profile_dir = Path(tempfile.gettempdir()) / f"lo_profile_{uuid.uuid4().hex}"
    try:
        env = prepare_font_environment()
        result = None
        for cmd in ("soffice", "libreoffice"):
            cmd_args = [
                cmd,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nodefault",
                # as_uri() gera file:///C:/... no Windows e file:///tmp/... no
                # Linux; "file://" + caminho cru falha silenciosamente no Windows
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--convert-to", filter_name,
                "--outdir", str(output_dir),
                str(input_path),
            ]
            result = run_lo_subprocess_with_backoff(cmd_args, env=env)
            if result and result.returncode == 0:
                break

        out_path = output_dir / (input_path.stem + "." + ext)
        if result is None:
            st.error("❌ O LibreOffice não respondeu a tempo. Tente um arquivo menor.")
            raise ConversionError("timeout")
        if not out_path.exists() or out_path.stat().st_size == 0:
            # "source file could not be loaded": a libcdr não interpretou este CDR
            st.error("❌ Não foi possível ler este CDR. A versão pode ser muito antiga, "
                     "muito nova, ou o arquivo está corrompido.")
            raise ConversionError("libcdr não carregou")

        log = (result.stdout or "") + (result.stderr or "")
        m = _LOADED_AS.search(log)
        if m and m.group(1) != "Draw":
            st.error("❌ Este arquivo não é um desenho do CorelDRAW.")
            raise ConversionError(f"carregado como {m.group(1)}")
        return out_path
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def convert_cdr_to_pdf(input_path: Path, output_dir: Path):
    """CDR -> ODG -> (recortes de imagem restaurados) -> PDF.
    A libcdr descarta o recorte das fotos (PowerClip/crop) e entrega a
    imagem inteira por cima do desenho; o ODG intermediário guarda o
    retângulo do recorte e o odg_crop o reaplica antes do PDF.
    Devolve (pdf_path, imagens_recortadas)."""
    odg_path = run_soffice_convert(input_path, output_dir, "odg:draw8", "odg")
    try:
        cropped = restore_image_crops(odg_path)
    except Exception:
        cropped = 0  # ODG intocado: melhor a imagem inteira do que nenhuma
    pdf_path = run_soffice_convert(odg_path, output_dir, "pdf:draw_pdf_Export", "pdf")
    return pdf_path, cropped


# ---------------------------------------------------------------------------
# ETAPA 2: PDF -> PNG COM O PyMuPDF (1ª página, 4000 px no lado maior)
# ---------------------------------------------------------------------------
def art_bbox(page):
    """Retângulo que envolve tudo que está desenhado na página (vetores,
    textos e imagens), limitado à página. O PNG é da ARTE, não da folha:
    um crachá numa página A4 não vira uma imagem quase toda branca."""
    rect = pymupdf.Rect()
    page_area = page.rect.get_area()
    # extended=True traz o clip vigente ("scissor") de cada desenho: as faixas
    # de um degradê são maiores que a forma que preenchem e só o clip as
    # limita. Sem isso a arte "cresce" até o retângulo do degradê.
    clips = []  # pilha de clips por nível: um clip vale para os desenhos mais fundos que o seguem
    for d in page.get_drawings(extended=True):
        level = d.get("level", 0)
        del clips[level:]
        if d.get("type") == "clip":
            scissor = d.get("scissor")
            clips.append(pymupdf.Rect(scissor) if scissor else page.rect)
            continue
        r = pymupdf.Rect(d["rect"])
        for c in clips:
            r &= c
        if r.is_empty:
            continue
        if r.get_area() >= page_area * 0.95:
            continue  # fundo da página (a libcdr emite um retângulo branco)
        rect |= r
    for b in page.get_text("blocks"):
        rect |= pymupdf.Rect(b[:4])
    for img in page.get_images(full=False):
        for r in page.get_image_rects(img[0]):
            rect |= r
    rect &= page.rect
    if rect.is_empty or rect.width < ART_MIN_PT or rect.height < ART_MIN_PT:
        return page.rect
    margin = max(rect.width, rect.height) * ART_MARGIN
    rect = pymupdf.Rect(rect.x0 - margin, rect.y0 - margin, rect.x1 + margin, rect.y1 + margin) & page.rect
    return rect


def pdf_to_png(pdf_path: Path):
    """Devolve (png_bytes, info). info = páginas, tamanho da arte e da página em cm."""
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        st.error("❌ O desenho foi lido, mas a saída veio corrompida.")
        raise ConversionError("pdf inválido")
    try:
        if doc.page_count == 0:
            st.error("❌ O desenho não tem nenhuma página.")
            raise ConversionError("sem páginas")
        page = doc[0]
        width_pt, height_pt = page.rect.width, page.rect.height
        if width_pt <= 0 or height_pt <= 0:
            st.error("❌ A página do desenho tem tamanho inválido.")
            raise ConversionError("página sem tamanho")

        has_text = bool(page.get_text("text").strip())
        has_images = bool(page.get_images(full=False))
        has_drawings = has_text or has_images or bool(page.get_drawings())
        if not has_drawings:
            # o LibreOffice abriu, mas a libcdr devolveu página vazia: sucesso
            # parcial sem sinal — o modo de falha mais comum desta conversão
            st.error("❌ O desenho abriu, mas a página saiu vazia: o leitor não "
                     "conseguiu interpretar o conteúdo deste CDR.")
            raise ConversionError("página vazia")

        clip = art_bbox(page)
        zoom = LONG_SIDE_PX / max(clip.width, clip.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
        png = pix.tobytes("png")
        info = {
            "pages": doc.page_count,
            "width_cm": clip.width / 72 * 2.54,
            "height_cm": clip.height / 72 * 2.54,
            "page_width_cm": width_pt / 72 * 2.54,
            "page_height_cm": height_pt / 72 * 2.54,
            "cropped_to_art": clip != page.rect,
            "has_text": has_text,
            "has_images": has_images,
            # nomes das fontes embutidas no PDF: prova de qual fonte o
            # LibreOffice usou de fato (ex.: "Impact" e não a substituta)
            "fonts": sorted({f[3].split("+")[-1] for f in page.get_fonts(full=False)}),
            # amostra do texto extraível (só para testes/diagnóstico)
            "text": page.get_text("text")[:2000],
        }
        return png, info
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# PIPELINE COMPLETO
# ---------------------------------------------------------------------------
def convert_cdr_to_png(input_file: str):
    """Converte um .cdr em PNG. Pasta de trabalho própria por chamada e
    apagada no finally, mesmo em erro."""
    input_path = Path(input_file)
    work_dir = Path(tempfile.gettempdir()) / f"cdr_{uuid.uuid4().hex}"
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        kind, label = inspect_header(input_path.read_bytes())
        if kind != "cdr":
            st.error(f"❌ Este arquivo não é um desenho do CorelDRAW ({label}).")
            raise ConversionError(f"não é cdr: {label}")

        with get_conversion_slots():
            pdf_path, cropped = convert_cdr_to_pdf(input_path, work_dir)
            png, info = pdf_to_png(pdf_path)

        info["version"] = label
        info["cropped_images"] = cropped
        return png, info
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CACHE DE CONVERSÃO (TTL 1h)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, max_entries=6, show_spinner=False)
def convert_upload_to_png(file_name: str, file_bytes: bytes):
    """Converte com cache por conteúdo: clicar em "Baixar PNG" dispara um
    rerun do script e, sem cache, o mesmo arquivo seria reconvertido do
    zero pelo LibreOffice a cada clique. Falhas levantam exceção de
    propósito — exceção não entra no cache, então erros transientes não
    ficam "grudados" por 1h."""
    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / (Path(file_name).name or "desenho.cdr")
        input_path.write_bytes(file_bytes)
        return convert_cdr_to_png(str(input_path))


# ---------------------------------------------------------------------------
# INTERFACE PRINCIPAL
# ---------------------------------------------------------------------------
def main():
    cleanup_stale_artifacts()
    uploaded_file = st.file_uploader(
        "Arraste e solte seu arquivo aqui",
        type=["cdr"],
        help="Arquivo CDR (CorelDRAW). Máximo: 200MB",
        label_visibility="collapsed",
    )

    if uploaded_file is None:
        return

    if (uploaded_file.size / (1024 * 1024)) > 200:
        st.error("❌ Arquivo muito grande! Máximo: 200MB")
        st.stop()

    ext = Path(uploaded_file.name).suffix.lower()
    if ext != SOURCE_EXT:
        st.error("❌ Formato não suportado.")
        return

    with st.spinner(f"Convertendo para {TARGET_EXT.upper()}..."):
        try:
            png_bytes, info = convert_upload_to_png(uploaded_file.name, uploaded_file.getvalue())
        except ConversionError:
            return

    st.success("✅ Conversão concluída!")
    st.image(png_bytes, use_column_width=True)

    size = f"{info['width_cm']:.1f} × {info['height_cm']:.1f} cm".replace(".", ",")
    detail = f"CorelDRAW {info['version']} · arte {size}"
    if info["cropped_to_art"]:
        page_size = f"{info['page_width_cm']:.1f} × {info['page_height_cm']:.1f} cm".replace(".", ",")
        detail += f" (página {page_size})"
    if info["cropped_images"]:
        n = info["cropped_images"]
        detail += f" · recorte de {n} {'imagens' if n > 1 else 'imagem'} restaurado"
    st.caption(detail)
    if info["pages"] > 1:
        st.warning(f"⚠️ O desenho tem {info['pages']} páginas; só a primeira foi convertida.")

    st.download_button(
        label=f"📥 Baixar {TARGET_EXT.upper()}",
        data=png_bytes,
        file_name=Path(uploaded_file.name).stem + "." + TARGET_EXT,
        mime=TARGET_MIME,
        type="primary",
        use_container_width=True,
    )


if __name__ == "__main__":
    main()
