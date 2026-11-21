from urllib.parse import urlsplit

from google.cloud import vision


class ErroBuscaWeb(Exception):
    pass


def _url_web_segura(url: str) -> bool:
    try:
        partes = urlsplit(url)
    except ValueError:
        return False
    return partes.scheme in {"http", "https"} and bool(partes.netloc)


def buscar_na_web(
    conteudo: bytes,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    cliente = vision.ImageAnnotatorClient()
    resposta = cliente.web_detection(
        image=vision.Image(content=conteudo),
        timeout=30,
    )
    if resposta.error.message:
        raise ErroBuscaWeb(resposta.error.message)

    deteccao = resposta.web_detection
    paginas = []
    urls_paginas = set()
    for pagina in deteccao.pages_with_matching_images:
        if pagina.url not in urls_paginas and _url_web_segura(pagina.url):
            imagem_pagina = next(
                (
                    imagem.url
                    for imagem in (
                        *pagina.full_matching_images,
                        *pagina.partial_matching_images,
                    )
                    if _url_web_segura(imagem.url)
                ),
                "",
            )
            paginas.append(
                {
                    "url": pagina.url,
                    "titulo": pagina.page_title or "Página com imagem correspondente",
                    "miniatura": imagem_pagina,
                }
            )
            urls_paginas.add(pagina.url)

    imagens = []
    urls_imagens = set()
    grupos = (
        ("Correspondência exata", deteccao.full_matching_images),
        ("Correspondência parcial", deteccao.partial_matching_images),
        ("Visualmente semelhante", deteccao.visually_similar_images),
    )
    for tipo, resultados in grupos:
        for imagem in resultados:
            if imagem.url not in urls_imagens and _url_web_segura(imagem.url):
                imagens.append({"url": imagem.url, "tipo": tipo})
                urls_imagens.add(imagem.url)

    return paginas, imagens


