import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit


def normalizar_url_pagina(valor: str) -> str:
    entrada = valor.strip()
    if not entrada or any(caractere.isspace() for caractere in entrada):
        raise ValueError("Informe a URL exata da página oficial.")

    url = entrada if "://" in entrada else f"https://{entrada}"
    try:
        partes = urlsplit(url)
        if partes.scheme.lower() not in {"http", "https"}:
            raise ValueError("esquema não suportado")
        if partes.username or partes.password or not partes.hostname:
            raise ValueError("URL inválida")
        host = partes.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        porta = partes.port
    except (UnicodeError, ValueError) as erro:
        raise ValueError("Informe uma URL válida começando com http:// ou https://.") from erro

    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or "." not in host:
            raise ValueError("A URL precisa conter um domínio válido.")
        rotulos = host.split(".")
        if any(
            not rotulo
            or len(rotulo) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", rotulo)
            for rotulo in rotulos
        ):
            raise ValueError("O domínio da URL não é válido.")

    if ":" in host:
        host = f"[{host}]"
    autoridade = host
    if porta is not None and porta not in {80, 443}:
        autoridade = f"{autoridade}:{porta}"

    caminho = partes.path.rstrip("/") or "/"
    return urlunsplit(("https", autoridade, caminho, partes.query, ""))


def url_pagina_da_busca(url: str) -> str:
    try:
        return normalizar_url_pagina(url)
    except ValueError:
        return ""


def usuario_tiktok(url: str) -> str:
    """Retorna o @usuario se a URL for o perfil ou um vídeo dele no TikTok."""
    normalizada = url_pagina_da_busca(url)
    if not normalizada:
        return ""
    partes = urlsplit(normalizada)
    if partes.hostname not in {"tiktok.com", "www.tiktok.com"}:
        return ""
    segmentos = [s for s in partes.path.split("/") if s]
    if not segmentos or not re.fullmatch(r"@[\w.]+", segmentos[0]):
        return ""
    if len(segmentos) > 1 and segmentos[1] != "video":
        return ""
    return segmentos[0].lower()


def _chave_oficial(url: str) -> str:
    usuario = usuario_tiktok(url)
    return f"tiktok:{usuario}" if usuario else url_pagina_da_busca(url)


def marcar_resultados(
    resultados: list[dict],
    paginas_oficiais,
    criadora_id: int | None = None,
) -> list[dict]:
    criadoras_por_url: dict[str, list[str]] = {}
    for pagina in paginas_oficiais:
        if pagina["criadora_id"] is None:
            continue
        if criadora_id is not None and pagina["criadora_id"] != criadora_id:
            continue
        url_oficial = _chave_oficial(pagina["url"])
        if url_oficial:
            nomes = criadoras_por_url.setdefault(url_oficial, [])
            nome_criadora = pagina["criadora"]
            if nome_criadora not in nomes:
                nomes.append(nome_criadora)

    marcados = []
    for resultado in resultados:
        item = dict(resultado)
        url = _chave_oficial(item.get("url") or item.get("link") or "")
        item["criadoras_oficiais"] = criadoras_por_url.get(url, [])
        marcados.append(item)
    return marcados


def _mesma_ou_filha(chave: str, chave_oficial: str) -> bool:
    if chave == chave_oficial:
        return True
    if chave.startswith("tiktok:") or chave_oficial.startswith("tiktok:"):
        return False
    pagina = urlsplit(chave)
    oficial = urlsplit(chave_oficial)
    if oficial.query or pagina.netloc != oficial.netloc:
        return False
    return pagina.path.startswith(oficial.path.rstrip("/") + "/")


def proprietario_pagina_oficial(url: str, paginas_oficiais, user_id: int) -> str:
    """Classifica a URL frente às páginas oficiais, incluindo páginas filhas."""
    pagina_normalizada = _chave_oficial(url)
    if not pagina_normalizada:
        return "desconhecido"

    donos = {
        pagina["user_id"]
        for pagina in paginas_oficiais
        if (chave := _chave_oficial(pagina["url"]))
        and _mesma_ou_filha(pagina_normalizada, chave)
    }
    if not donos:
        return "nao_oficial"
    if donos == {user_id}:
        return "proprio"
    return "terceiro_ou_incerto"


_LISTAGENS = {
    ("tiktok.com", "search"), ("tiktok.com", "tag"), ("tiktok.com", "discover"),
    ("tiktok.com", "explore"), ("tiktok.com", "music"),
    ("instagram.com", "explore"), ("youtube.com", "results"),
    ("youtube.com", "hashtag"), ("google.com", "search"),
}


def pagina_de_listagem(url: str) -> bool:
    """True para páginas de busca/tag/descoberta, que mostram vários autores."""
    normalizada = url_pagina_da_busca(url)
    if not normalizada:
        return False
    partes = urlsplit(normalizada)
    host = (partes.hostname or "").removeprefix("www.")
    segmentos = [x for x in partes.path.split("/") if x]
    if not segmentos:
        return False
    primeiro = segmentos[0].lower()
    if host == "tiktok.com" and len(segmentos) > 1 and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", primeiro):
        primeiro = segmentos[1].lower()
    return (host, primeiro) in _LISTAGENS
