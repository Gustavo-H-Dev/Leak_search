"""PDF simples, sem dependencia externa, para exportar metadados.

Gera um A4 com Helvetica, quebra de linha por largura aproximada e varias
paginas. So texto: nada de imagem, link ou HTML.
"""

from datetime import datetime, timezone

LARGURA_PAGINA = 595  # A4 em pontos
ALTURA_PAGINA = 842
MARGEM = 48
LINHA = 14
MAX_CARACTERES = 92


def _escapar(texto) -> str:
    return (
        str(texto or "")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _quebrar(texto):
    texto = " ".join(str(texto or "").split())
    if not texto:
        return [""]
    linhas = []
    atual = ""
    for palavra in texto.split(" "):
        candidato = f"{atual} {palavra}".strip()
        if len(candidato) <= MAX_CARACTERES:
            atual = candidato
        else:
            if atual:
                linhas.append(atual)
            atual = palavra[:MAX_CARACTERES]
    if atual:
        linhas.append(atual)
    return linhas


def _paginas(titulo, subtitulo, blocos):
    """Cada bloco vira uma lista de linhas; o titulo so aparece na primeira."""
    paginas = []
    conteudo = [
        ("titulo", titulo),
        ("subtitulo", subtitulo),
        ("subtitulo", ""),
    ]
    for bloco in blocos:
        for linha in _quebrar(bloco):
            conteudo.append(("texto", linha))
    limite = (ALTURA_PAGINA - 2 * MARGEM - 24) // LINHA
    for inicio in range(0, len(conteudo), limite):
        paginas.append(conteudo[inicio : inicio + limite])
    return paginas or [[]]


def _stream(pagina):
    partes = ["BT", f"/F1 10 Tf", f"{MARGEM} {ALTURA_PAGINA - MARGEM} Td"]
    primeira = True
    for tipo, texto in pagina:
        if tipo == "titulo":
            partes.append("/F1 16 Tf")
        elif tipo == "subtitulo":
            partes.append("/F1 10 Tf")
        else:
            partes.append("/F1 10 Tf")
        if not primeira:
            partes.append(f"0 -{LINHA} Td")
        partes.append(f"({_escapar(texto)}) Tj")
        primeira = False
    partes.append("ET")
    return "\n".join(partes)


def gerar_pdf(titulo: str, subtitulo: str, blocos) -> bytes:
    paginas = _paginas(titulo, subtitulo, blocos)
    objetos = []
    total = len(paginas)
    # 1 catalogo, 2 pages, 3 fonte, depois pagina/conteudo por pagina.
    ids_paginas = []
    for indice in range(total):
        ids_paginas.append(4 + indice * 2)
    objetos.append("<< /Type /Catalog /Pages 2 0 R >>")
    objetos.append(
        "<< /Type /Pages /Kids [{}] /Count {} >>".format(
            " ".join(f"{ident} 0 R" for ident in ids_paginas), total
        )
    )
    objetos.append(
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    for indice, pagina in enumerate(paginas):
        stream = _stream(pagina)
        objetos.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {} {}] "
            "/Resources << /Font << /F1 3 0 R >> >> /Contents {} 0 R >>".format(
                LARGURA_PAGINA, ALTURA_PAGINA, 5 + indice * 2
            )
        )
        objetos.append(
            f"<< /Length {len(stream.encode('cp1252', 'replace'))} >>\nstream\n{stream}\nendstream"
        )

    saida = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for numero, objeto in enumerate(objetos, start=1):
        offsets.append(len(saida))
        saida += f"{numero} 0 obj\n{objeto}\nendobj\n".encode("cp1252", "replace")
    inicio_xref = len(saida)
    saida += f"xref\n0 {len(objetos) + 1}\n".encode("ascii")
    saida += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        saida += f"{offset:010d} 00000 n \n".encode("ascii")
    saida += (
        f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{inicio_xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(saida)


def subtitulo_padrao() -> str:
    return (
        "Metadados exportados em "
        + datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        + ". Sem trechos, imagens ou links."
    )
