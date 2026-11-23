"""Exportação do documento digitalizado em PDF pesquisável.

Um PDF pesquisável tem duas camadas na mesma página:
    1. a IMAGEM colorida do documento digitalizado (o que a pessoa vê);
    2. o TEXTO reconhecido pelo OCR, invisível, posicionado sobre cada palavra
       da imagem.

Assim o arquivo fica idêntico ao papel (assinaturas, carimbos, fotos e tabelas
preservados), mas dá para buscar (Ctrl+F), selecionar e copiar o texto. É o
formato usado por scanners profissionais.

Por que o PDF é montado aqui, e não pelo renderizador "pdf" do Tesseract
-------------------------------------------------------------------------
O Tesseract embute no PDF exatamente a imagem que ele leu. Mas a leitura é bem
melhor numa versão tratada (cinza, ampliada, com a iluminação corrigida), que
não é a que queremos mostrar. Então:
    - o OCR roda na versão tratada e devolve a caixa de cada palavra;
    - o PDF recebe a imagem colorida original (em JPEG, arquivo pequeno) e as
      palavras, com as caixas convertidas para a escala da página.
O formato PDF é texto simples com alguns objetos, então não é preciso nenhuma
biblioteca extra.
"""

import cv2
import pytesseract

from nucleo.ocr import _corrigir_iluminacao, _escolher_idioma, _remover_alpha, _validar

# Largura de uma folha A4 em pontos (1 pt = 1/72 polegada). A página do PDF
# tem a largura de uma A4 e a altura proporcional ao documento.
LARGURA_PAGINA_PT = 595.28

# Largura (px) desejada da imagem lida pelo OCR (~A4 a 300 DPI). Imagens
# menores são ampliadas (até 4x): o Tesseract erra muito com letras miúdas.
LARGURA_OCR_PX = 2480

# Qualidade do JPEG embutido (0 a 100). Mais alto = mais nítido e mais pesado.
QUALIDADE_JPEG = 90

# Na fonte Courier (monoespaçada) cada caractere ocupa 0,6 do tamanho da fonte.
# Isso permite calcular a largura de cada palavra sem tabela de métricas.
LARGURA_CARACTERE_COURIER = 0.6


def _palavras_ocr(imagem_bgr, idioma):
    """Roda o OCR na versão tratada e devolve [(texto, x, y, largura, altura)].

    As caixas voltam na escala da imagem ORIGINAL (em px), não da ampliada.
    """
    cinza = cv2.cvtColor(imagem_bgr, cv2.COLOR_BGR2GRAY)
    escala = min(4.0, max(1.0, LARGURA_OCR_PX / cinza.shape[1]))
    if escala > 1:
        cinza = cv2.resize(cinza, None, fx=escala, fy=escala, interpolation=cv2.INTER_CUBIC)
    if cinza.max() > cinza.min():
        cinza = _corrigir_iluminacao(cinza)

    dados = pytesseract.image_to_data(
        cinza, lang=idioma, config="--oem 1 --psm 6 --dpi 300",
        output_type=pytesseract.Output.DICT, timeout=120,
    )
    palavras = []
    for i, texto in enumerate(dados["text"]):
        texto = texto.strip()
        if not texto or float(dados["conf"][i]) < 0:
            continue
        palavras.append((texto,
                         dados["left"][i] / escala, dados["top"][i] / escala,
                         dados["width"][i] / escala, dados["height"][i] / escala))
    return palavras


def _texto_pdf(texto):
    """Converte o texto para uma string literal do PDF (codificação WinAnsi).

    WinAnsi (cp1252) tem todos os acentos do português. Caracteres fora dela
    viram "?". Parênteses e barra invertida precisam de escape.
    """
    dados = texto.encode("cp1252", errors="replace")
    dados = dados.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return b"(" + dados + b")"


def _conteudo_pagina(palavras, largura_pt, altura_pt, pt_por_px):
    """Monta os comandos de desenho da página: a imagem e o texto invisível."""
    linhas = [f"q {largura_pt:.2f} 0 0 {altura_pt:.2f} 0 0 cm /Im0 Do Q".encode(),
              b"BT 3 Tr"]  # 3 Tr = modo de texto invisível
    for texto, x, y, largura, altura in palavras:
        tamanho = max(1.0, altura * pt_por_px)
        # Estica/encolhe a palavra (Tz, em %) para ocupar a largura da caixa.
        largura_natural = LARGURA_CARACTERE_COURIER * tamanho * len(texto)
        horizontal = 100.0 * (largura * pt_por_px) / largura_natural
        # PDF conta y de baixo para cima; a linha de base fica perto do pé da caixa.
        base_x = x * pt_por_px
        base_y = altura_pt - (y + altura) * pt_por_px + 0.15 * tamanho
        linhas.append(f"/F1 {tamanho:.2f} Tf {horizontal:.2f} Tz "
                      f"1 0 0 1 {base_x:.2f} {base_y:.2f} Tm ".encode()
                      + _texto_pdf(texto + " ") + b" Tj")
    linhas.append(b"ET")
    return b"\n".join(linhas)


def _montar_pdf(jpeg, largura_px, altura_px, cinza, conteudo, largura_pt, altura_pt):
    """Escreve o arquivo PDF (uma página) com seus objetos e a tabela xref."""
    espaco_cor = b"/DeviceGray" if cinza else b"/DeviceRGB"
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {largura_pt:.2f} {altura_pt:.2f}] "
         "/Resources << /XObject << /Im0 4 0 R >> /Font << /F1 6 0 R >> >> "
         "/Contents 5 0 R >>").encode(),
        (f"<< /Type /XObject /Subtype /Image /Width {largura_px} /Height {altura_px} "
         f"/ColorSpace ").encode() + espaco_cor
        + f" /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n".encode()
        + jpeg + b"\nendstream",
        f"<< /Length {len(conteudo)} >>\nstream\n".encode() + conteudo + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
    ]

    saida = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    posicoes = []
    for numero, corpo in enumerate(objetos, 1):
        posicoes.append(len(saida))
        saida += f"{numero} 0 obj\n".encode() + corpo + b"\nendobj\n"

    inicio_xref = len(saida)
    saida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    for posicao in posicoes:
        saida += f"{posicao:010d} 00000 n \n".encode()
    saida += (f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\n"
              f"startxref\n{inicio_xref}\n%%EOF\n").encode()
    return bytes(saida)


def gerar_pdf_pesquisavel(imagem, idioma="por+eng"):
    """Gera um PDF pesquisável a partir de `imagem` (BGR, BGRA ou cinza do OpenCV).

    Retorna o conteúdo do PDF em bytes (para gravar em arquivo com "wb").
    """
    _validar(imagem)
    idioma = _escolher_idioma(idioma)

    imagem = _remover_alpha(imagem)
    cinza = imagem.ndim == 2
    bgr = cv2.cvtColor(imagem, cv2.COLOR_GRAY2BGR) if cinza else imagem

    altura_px, largura_px = imagem.shape[:2]
    pt_por_px = LARGURA_PAGINA_PT / largura_px
    largura_pt, altura_pt = LARGURA_PAGINA_PT, altura_px * pt_por_px

    palavras = _palavras_ocr(bgr, idioma)
    conteudo = _conteudo_pagina(palavras, largura_pt, altura_pt, pt_por_px)

    sucesso, jpeg = cv2.imencode(".jpg", imagem, [cv2.IMWRITE_JPEG_QUALITY, QUALIDADE_JPEG])
    if not sucesso:
        raise RuntimeError("Não foi possível comprimir a imagem para o PDF.")

    return _montar_pdf(jpeg.tobytes(), largura_px, altura_px, cinza, conteudo,
                       largura_pt, altura_pt)
