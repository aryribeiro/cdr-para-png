# 🎨 Conversor de CDR para PNG

Aplicação web em Python/Streamlit que converte desenhos **CDR (CorelDRAW) para PNG**, sem CorelDRAW.

## 🎯 O que faz

| Entrada | Saída |
| --- | --- |
| `.cdr` (CorelDRAW 7 a X7; versões mais antigas e mais novas dependem do arquivo) | **`.png`** da arte da primeira página (recortado ao que está desenhado, com folga de 3%; fundo branco, 4000 px no lado maior) |

Escopo único e fixo — este app não lida com nenhum outro formato de entrada ou saída.

- Interface de tela única (upload → converter → prévia → baixar)
- Mostra a versão do CorelDRAW e o tamanho da página; avisa quando há mais de uma página
- Processamento em diretórios temporários — nenhum arquivo é armazenado

## ⚙️ Como converte

1. **LibreOffice Draw** (headless) lê o CDR com a biblioteca **libcdr** e grava um ODG.
2. **odg_crop.py** reaplica os recortes de imagem que a libcdr perde: no Corel, uma foto recortada (PowerClip ou ferramenta de corte) chega ao ODG como um polígono invisível com o retângulo do recorte seguido da imagem inteira, que cobre o resto do desenho. O módulo corta os pixels na proporção do polígono e encolhe a moldura. Recortes não retangulares viram o retângulo envolvente.
3. **LibreOffice** exporta o ODG corrigido para PDF, enxergando as fontes de `static/fonts/` (181 arquivos, mesma coleção do app libreoffice) via fontconfig. Sem isso, uma fonte ausente no servidor vira outra de largura diferente e o texto estoura a arte.
4. **PyMuPDF** rasteriza a primeira página em PNG na resolução calculada.

Limites honestos, medidos num corpus de 84 CDR reais (do CorelDRAW 7 ao X4+): cores CMYK e Pantone saem em RGB; efeitos exclusivos do Corel (envelope, lente, extrusão) podem não aparecer; fontes ausentes no servidor são substituídas; texto cirílico de alguns arquivos das versões 8 e 9 sai como "?????" (limitação da libcdr). Um SVG, PDF ou ODG renomeado para `.cdr` é recusado antes de chegar ao LibreOffice, porque ele "converteria" sem nunca ler CorelDRAW. Se a libcdr abrir o arquivo mas devolver página vazia, o app avisa em vez de entregar um PNG em branco.

## 🚀 Rodar localmente

Pré-requisitos: Python 3.10+ e LibreOffice instalado (com o componente Draw).

```bash
pip install -r requirements.txt
streamlit run app.py
```

Abre em `http://localhost:8501`.

## 🧪 Testes

```bash
pip install pytest
pytest -q
```

Os testes cobrem o algoritmo de versão (igual ao da libcdr), a detecção de impostores e a conversão de ponta a ponta com CDR reais de `tests/fixtures/` (corpus público de testes do LibreOffice). Os PNG gerados ficam em `tests/output/`.

Para provar o ambiente de deploy (Debian com os pacotes do `packages.txt`):

```bash
docker build -f tests/Dockerfile.smoke -t cdr-smoke . && docker run --rm cdr-smoke
```

## ☁️ Deploy no Streamlit Cloud

1. Faça push para o GitHub
2. Em [share.streamlit.io](https://share.streamlit.io), conecte o repositório
3. Em **Advanced settings**, escolha **Python 3.13** (ou 3.12). Com Python 3.14 a instalação falha: o Streamlit 1.39 exige pillow abaixo da versão 11, que não tem pacote pronto para 3.14. A versão do Python não pode ser trocada depois; é preciso apagar o app e implantar de novo.
4. O `packages.txt` (incluído) instala o LibreOffice Draw, que já traz a libcdr, e as fontes substitutas
5. Deploy

App no ar: https://cdr-para-png.streamlit.app/

## 📋 Estrutura

```
cdr-para-png/
├── app.py              # Aplicação principal
├── requirements.txt    # streamlit, pymupdf
├── packages.txt        # Pacotes do sistema (LibreOffice Draw, fontes)
├── odg_crop.py         # Reaplica recortes de imagem perdidos pela libcdr
├── static/fonts/       # Fontes que o LibreOffice usa na conversão
├── tests/              # pytest + fixtures CDR reais + Dockerfile do smoke
├── NOTICE.md           # Licenças dos componentes
└── README.md
```

## 🛠️ Tecnologias

- **Streamlit** — interface web
- **LibreOffice Draw** + **libcdr** (headless) — leitura do CorelDRAW
- **PyMuPDF** — rasterização em PNG

## 🔒 Privacidade

Os arquivos são processados em diretórios temporários e removidos após a conversão. Nada é armazenado permanentemente.

---

Desenvolvido com ❤️ usando Python e Streamlit.
