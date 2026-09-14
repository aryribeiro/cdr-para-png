# NOTICE

O código deste repositório (`app.py`, testes e documentação) é distribuído sob a licença MIT (ver `LICENSE`).

Este projeto depende de componentes de terceiros com licenças próprias. Nenhum deles é redistribuído aqui: são instalados pelo sistema (`packages.txt`) ou pelo pip (`requirements.txt`).

| Componente | Uso | Licença |
| --- | --- | --- |
| **LibreOffice Draw** com **libcdr** | leitura do CDR e conversão para PDF, em processo separado | MPL-2.0 (LibreOffice, libcdr, librevenge) |
| **PyMuPDF** (MuPDF) | rasterização em PNG | AGPL-3.0 (Artifex). O uso comercial sem disponibilizar o código-fonte exige licença comercial da Artifex |
| **Streamlit** | interface web | Apache-2.0 |
| **defusedxml** | leitura segura do ODG intermediário | PSF-2.0 |
| **Liberation** e **DejaVu** (pacotes do sistema) | fontes substitutas para textos do desenho | SIL OFL 1.1 / Bitstream Vera |

A pasta `static/fonts/` traz a mesma coleção de fontes usada pelos apps irmãos do autor (Liberation, DejaVu, Carlito, Noto e fontes de sistema como Arial, Impact e Wingdings), para que os textos do desenho saiam com a fonte que o CorelDRAW usou. As famílias livres seguem suas licenças (SIL OFL 1.1, Bitstream Vera, Apache-2.0); as fontes de sistema são incluídas só para fidelidade de conversão e permanecem sob os termos de seus fabricantes.

Os arquivos CDR em `tests/fixtures/` vêm do corpus público de documentos de teste do projeto LibreOffice (https://dev-www.libreoffice.org/corpus/, `cdrfuzzer_seed_corpus.zip`) e são usados apenas para verificação automatizada.

CorelDRAW e CDR são marcas da Corel Corporation. Este projeto não é afiliado à Corel.
