"""Normalizacao de URL e pontuacao por regras dos candidatos de busca.

Nada aqui baixa midia: a decisao usa apenas URL, titulo, trecho e as listas de
arrobas/dominios oficiais ou de vazamento configuradas.
"""

import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PARAMETROS_RASTREIO = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "gclsrc",
    "dclid",
    "fbclid",
    "msclkid",
    "igshid",
    "igsh",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "ref_url",
    "source",
    "src",
    "_ga",
    "_gl",
}

PLATAFORMAS_OFICIAIS = {
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
}

PALAVRAS_SUSPEITAS = (
    "vazado",
    "vazada",
    "vazou",
    "leaked",
    "leak",
    "nude",
    "nudes",
    "naked",
    "onlyfans",
    "privacy",
    "pack",
    "conteudo completo",
    "conteúdo completo",
    "download",
    "xvideos",
    "porn",
)

_ROTAS_BUSCA = {
    "search",
    "busca",
    "buscar",
    "pesquisa",
    "pesquisar",
    "tag",
    "tags",
    "hashtag",
    "explore",
    "discover",
    "results",
    "resultados",
}
_DOMINIOS_BUSCA = {
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "search.yahoo.com",
    "yandex.com",
}


def _env_int(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, str(padrao)).strip())
    except (TypeError, ValueError):
        return padrao


def limiar_vazamento() -> int:
    return _env_int("PONTUACAO_LIMIAR_VAZAMENTO", 60)


def limiar_revisao() -> int:
    return _env_int("PONTUACAO_LIMIAR_REVISAO", 30)


def normalizar_url(valor: str) -> str:
    """URL https, host minusculo e sem parametros de rastreio."""
    entrada = (valor or "").strip()
    if not entrada:
        return ""
    if "://" not in entrada:
        entrada = f"https://{entrada}"
    try:
        partes = urlsplit(entrada)
        if partes.scheme.lower() not in {"http", "https"} or not partes.hostname:
            return ""
        if partes.username or partes.password:
            return ""
        host = partes.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        host = host.removeprefix("www.")
        porta = partes.port
    except (UnicodeError, ValueError):
        return ""
    if not host:
        return ""
    autoridade = host if porta in (None, 80, 443) else f"{host}:{porta}"
    consulta = [
        (chave, valor_parametro)
        for chave, valor_parametro in parse_qsl(partes.query, keep_blank_values=True)
        if chave.lower() not in PARAMETROS_RASTREIO
    ]
    consulta.sort()
    caminho = partes.path.rstrip("/") or "/"
    return urlunsplit(("https", autoridade, caminho, urlencode(consulta), ""))


def dominio_de(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _segmentos(url: str) -> list[str]:
    try:
        return [parte for parte in urlsplit(url).path.split("/") if parte]
    except ValueError:
        return []


def _arroba_em_texto(texto: str, arroba: str) -> bool:
    if not texto or not arroba:
        return False
    padrao = rf"(?<![\w.])@?{re.escape(arroba)}(?![\w.])"
    return re.search(padrao, texto, re.IGNORECASE) is not None


def pagina_de_busca(url: str) -> bool:
    """Paginas que listam varios autores e nao provam publicacao."""
    host = dominio_de(url)
    segmentos = _segmentos(url)
    if host in _DOMINIOS_BUSCA and (
        any(segmento.lower() in _ROTAS_BUSCA for segmento in segmentos)
        or "q=" in (urlsplit(url).query or "")
    ):
        return True
    if segmentos and segmentos[0].lower() in _ROTAS_BUSCA:
        return True
    if segmentos and segmentos[0].lower() in {"@"}:
        return False
    consulta = urlsplit(url).query.lower()
    return bool(re.search(r"(^|&)(q|query|search_query)=", consulta)) and host in _DOMINIOS_BUSCA


def _plataforma_oficial(host: str) -> str:
    for dominio, plataforma in PLATAFORMAS_OFICIAIS.items():
        if host == dominio or host.endswith(f".{dominio}"):
            return plataforma
    return ""


def _perfil_da_plataforma(url: str, arroba: str) -> bool:
    plataforma = _plataforma_oficial(dominio_de(url))
    if not plataforma or not arroba:
        return False
    segmentos = _segmentos(url)
    if not segmentos:
        return False
    primeiro = segmentos[0].lstrip("@").lower()
    return primeiro == arroba.lower()


def _espelho_de_perfil(url: str, arroba: str) -> bool:
    segmentos = _segmentos(url)
    if not segmentos or not arroba:
        return False
    ultimo = segmentos[-1].lstrip("@").lower()
    if ultimo != arroba.lower():
        return False
    host = dominio_de(url)
    return not _plataforma_oficial(host)


def _pagina_oficial(url: str, paginas_oficiais) -> bool:
    for pagina in paginas_oficiais or ():
        oficial = pagina if isinstance(pagina, str) else pagina.get("url", "")
        oficial = normalizar_url(oficial)
        if not oficial or oficial == "/":
            continue
        if url == oficial or url.startswith(oficial.rstrip("/") + "/"):
            return True
    return False


def avaliar(
    url: str,
    titulo: str = "",
    trecho: str = "",
    arrobas=(),
    dominios_vazamento=(),
    paginas_oficiais=(),
) -> dict:
    """Nota 0-100, motivos e classificacao de um candidato."""
    normalizada = normalizar_url(url)
    if not normalizada:
        return {
            "nota": 0,
            "motivos": ["URL inválida ou sem domínio"],
            "classificacao": "falso_positivo",
        }

    host = dominio_de(normalizada)
    arrobas = [a.strip().lstrip("@").lower() for a in arrobas if a and a.strip()]
    dominios_vazamento = {
        d.strip().lower().removeprefix("www.") for d in dominios_vazamento if d and d.strip()
    }
    motivos = []

    if _pagina_oficial(normalizada, paginas_oficiais):
        return {
            "nota": 0,
            "motivos": ["página oficial cadastrada pela criadora"],
            "classificacao": "oficial",
        }

    for arroba in arrobas:
        if _perfil_da_plataforma(normalizada, arroba):
            return {
                "nota": 0,
                "motivos": [f"perfil oficial da própria criadora (@{arroba})"],
                "classificacao": "oficial",
            }

    if pagina_de_busca(normalizada):
        return {
            "nota": 0,
            "motivos": ["página de busca/lista com vários autores"],
            "classificacao": "falso_positivo",
        }

    for arroba in arrobas:
        if _espelho_de_perfil(normalizada, arroba):
            return {
                "nota": 0,
                "motivos": [f"espelho do perfil de @{arroba}"],
                "classificacao": "falso_positivo",
            }

    nota = 0
    texto = f"{titulo or ''} {trecho or ''}".lower()
    for arroba in arrobas:
        if _arroba_em_texto(f"{titulo or ''} {normalizada}", arroba):
            nota += 30
            motivos.append(f"@{arroba} aparece no título ou na URL")
            break

    if host in dominios_vazamento:
        nota += 40
        motivos.append(f"domínio de vazamento configurado ({host})")
    elif any(host.endswith(f".{dominio}") for dominio in dominios_vazamento):
        nota += 40
        motivos.append(f"subdomínio de vazamento configurado ({host})")

    encontradas = [palavra for palavra in PALAVRAS_SUSPEITAS if palavra in texto]
    if encontradas:
        nota += min(30, 10 * len(encontradas))
        motivos.append("termos suspeitos: " + ", ".join(encontradas[:4]))

    nota = max(0, min(100, nota))
    if nota >= limiar_vazamento():
        classificacao = "provavel_vazamento"
    elif nota >= limiar_revisao():
        classificacao = "indefinido"
    else:
        classificacao = "falso_positivo"

    if not motivos:
        motivos.append("sem sinais de vazamento nas regras atuais")
    return {"nota": nota, "motivos": motivos, "classificacao": classificacao}
