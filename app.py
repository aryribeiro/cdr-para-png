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

import pymupdf

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
# Cadeia: LibreOffice Draw (libcdr) -> PDF -> PyMuPDF -> PNG da 1ª página.
# ---------------------------------------------------------------------------
SOURCE_EXT = ".cdr"
TARGET_EXT = "png"
TARGET_MIME = "image/png"

LONG_SIDE_PX = 4000        # lado maior da imagem, em pixels
SOFFICE_TIMEOUT = 180      # segundos por tentativa


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


def convert_cdr_to_pdf(input_path: Path, output_dir: Path) -> Path:
    """Converte o .cdr em PDF com o LibreOffice Draw. Perfil de usuário
    próprio por execução: sem isso, conversões simultâneas falham caladas."""
    profile_dir = Path(tempfile.gettempdir()) / f"lo_profile_{uuid.uuid4().hex}"
    try:
        env = os.environ.copy()
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
                "--convert-to", "pdf:draw_pdf_Export",
                "--outdir", str(output_dir),
                str(input_path),
            ]
            result = run_lo_subprocess_with_backoff(cmd_args, env=env)
            if result and result.returncode == 0:
                break

        pdf_path = output_dir / (input_path.stem + ".pdf")
        if result is None:
            st.error("❌ O LibreOffice não respondeu a tempo. Tente um arquivo menor.")
            raise ConversionError("timeout")
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            # "source file could not be loaded": a libcdr não interpretou este CDR
            st.error("❌ Não foi possível ler este CDR. A versão pode ser muito antiga, "
                     "muito nova, ou o arquivo está corrompido.")
            raise ConversionError("libcdr não carregou")

        log = (result.stdout or "") + (result.stderr or "")
        m = _LOADED_AS.search(log)
        if m and m.group(1) != "Draw":
            st.error("❌ Este arquivo não é um desenho do CorelDRAW.")
            raise ConversionError(f"carregado como {m.group(1)}")
        return pdf_path
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# ETAPA 2: PDF -> PNG COM O PyMuPDF (1ª página, 4000 px no lado maior)
# ---------------------------------------------------------------------------
def pdf_to_png(pdf_path: Path):
    """Devolve (png_bytes, info). info = páginas, tamanho da página em cm."""
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

        zoom = LONG_SIDE_PX / max(width_pt, height_pt)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        png = pix.tobytes("png")
        info = {
            "pages": doc.page_count,
            "width_cm": width_pt / 72 * 2.54,
            "height_cm": height_pt / 72 * 2.54,
            "has_text": has_text,
            "has_images": has_images,
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
            pdf_path = convert_cdr_to_pdf(input_path, work_dir)
            png, info = pdf_to_png(pdf_path)

        info["version"] = label
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
    st.caption(f"CorelDRAW {info['version']} · {size}")
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
