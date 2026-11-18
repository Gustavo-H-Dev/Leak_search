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
        url_oficial = url_pagina_da_busca(pagina["url"])
        if url_oficial:
            nomes = criadoras_por_url.setdefault(url_oficial, [])
            nome_criadora = pagina["criadora"]
            if nome_criadora not in nomes:
                nomes.append(nome_criadora)

    marcados = []
    for resultado in resultados:
        item = dict(resultado)
        url = url_pagina_da_busca(item.get("url") or item.get("link") or "")
        item["criadoras_oficiais"] = criadoras_por_url.get(url, [])
        marcados.append(item)
    return marcados


def marcar_sites_pessoais(resultados: list[dict], sites_oficiais) -> list[dict]:
    urls_oficiais = {
        url_pagina_da_busca(site["url"])
        for site in sites_oficiais
        if url_pagina_da_busca(site["url"])
    }
    return [
        {
            **resultado,
            "site_oficial": url_pagina_da_busca(
                resultado.get("url") or resultado.get("link") or ""
            )
            in urls_oficiais,
        }
        for resultado in resultados
    ]
