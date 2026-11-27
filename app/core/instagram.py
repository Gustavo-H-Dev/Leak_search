"""Perfil publico do Instagram: postagens oficiais e arrobas citados na bio."""

import logging
import re

import httpx

from app.core import imagens, pontuacao, serpapi, sites_oficiais
from app.core.dominios_vazamento import DOMINIOS_VAZAMENTO_PADRAO

logger = logging.getLogger(__name__)

USER_AGENT_IMAGEM = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_RE_ARROBA = re.compile(r"(?<![\w.])@([A-Za-z0-9._]{1,30})")
_RESERVADOS = {
    "instagram",
    "insta",
    "tiktok",
    "x",
    "twitter",
    "youtube",
    "onlyfans",
    "privacy",
    "telegram",
    "facebook",
    "linktr",
    "beacons",
}
PLATAFORMAS_POR_DOMINIO = (
    ("instagram.com", "instagram"),
    ("tiktok.com", "tiktok"),
    ("x.com", "x"),
    ("twitter.com", "x"),
    ("youtube.com", "youtube"),
    ("onlyfans.com", "onlyfans"),
    ("privacy.com.br", "privacy"),
    ("telegram.me", "telegram"),
    ("t.me", "telegram"),
)


class BuscaInstagramIndisponivel(Exception):
    """Falha ao buscar postagens do Instagram, com mensagem pronta para o usuario."""


class PerfilInstagramNaoEncontrado(BuscaInstagramIndisponivel):
    """O Instagram respondeu e nao existe perfil publico com esse @."""


_RE_LINK_BIO = re.compile(
    r"(?<![\w@.-])(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(/[^\s\"'<>()\]]*)?",
    re.IGNORECASE,
)
_DOMINIOS_IGNORADOS = {
    "gmail.com",
    "hotmail.com",
    "outlook.com",
    "yahoo.com",
    "yahoo.com.br",
    "icloud.com",
    "protonmail.com",
    "live.com",
    "bol.com.br",
    "uol.com.br",
    "terra.com.br",
    *(dominio for dominio, _ in PLATAFORMAS_POR_DOMINIO),
    *DOMINIOS_VAZAMENTO_PADRAO,
}


def extrair_arrobas(bio: str, arroba_propria: str = "") -> list[dict]:
    """Arrobas citados na bio, com a plataforma quando o link deixa claro."""
    bio = bio or ""
    proprio = (arroba_propria or "").strip().lstrip("@").lower()
    encontrados: dict[tuple[str, str], dict] = {}

    for dominio, plataforma in PLATAFORMAS_POR_DOMINIO:
        padrao = rf"{re.escape(dominio)}/(?:@)?([A-Za-z0-9._]{{1,30}})"
        for achado in re.finditer(padrao, bio, re.IGNORECASE):
            arroba = achado.group(1).strip(".").lower()
            if arroba and arroba != proprio and arroba not in _RESERVADOS:
                encontrados[(plataforma, arroba)] = {
                    "plataforma": plataforma,
                    "arroba": arroba,
                    "origem": "bio do Instagram",
                }

    for achado in _RE_ARROBA.finditer(bio):
        arroba = achado.group(1).strip(".").lower()
        if not arroba or arroba == proprio or arroba in _RESERVADOS:
            continue
        chave = ("", arroba)
        encontrados.setdefault(
            chave,
            {"plataforma": "", "arroba": arroba, "origem": "bio do Instagram"},
        )

    return list(encontrados.values())


def extrair_links(bio: str) -> list[str]:
    """URLs de sites proprios citadas na bio (linktree, loja, site pessoal).

    Serve para reconhecer automaticamente as paginas oficiais da criadora sem
    que ela precise cadastrar site nenhum.
    """
    links: list[str] = []
    vistos: set[str] = set()
    for achado in _RE_LINK_BIO.finditer(bio or ""):
        dominio = achado.group(1).lower().removeprefix("www.")
        caminho = (achado.group(2) or "").rstrip(".,;:!?")
        if dominio in _DOMINIOS_IGNORADOS:
            continue
        normalizada = pontuacao.normalizar_url(f"https://{dominio}{caminho}")
        if not normalizada or normalizada in vistos:
            continue
        vistos.add(normalizada)
        links.append(normalizada)
        if len(links) >= 15:
            break
    return links


def baixar_hash_da_miniatura(url: str) -> str:
    """Baixa a miniatura em memoria e devolve o pHash; vazio quando falha."""
    if not url:
        return ""
    try:
        with httpx.Client(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT_IMAGEM},
        ) as cliente:
            resposta = cliente.get(url)
        if resposta.status_code != 200:
            logger.warning("Miniatura do Instagram respondeu HTTP %s", resposta.status_code)
            return ""
        tipo = resposta.headers.get("content-type", "")
        if not tipo.lower().startswith("image/"):
            logger.warning("Miniatura do Instagram nao e imagem (%s)", tipo)
            return ""
        return imagens.calcular_phash(resposta.content)
    except Exception:
        logger.exception("Falha ao baixar miniatura do Instagram")
        return ""


def buscar_perfil(handle: str) -> dict:
    try:
        resposta_api = serpapi.pesquisar("instagram_profile", profile_id=handle)
    except serpapi.SemResultados as erro:
        raise PerfilInstagramNaoEncontrado(
            f"O Instagram nao encontrou o perfil @{handle}. Confira a grafia do "
            "arroba ou use um perfil com publicacoes publicas."
        ) from erro
    except serpapi.SerpApiNaoConfigurada as erro:
        raise BuscaInstagramIndisponivel(str(erro)) from erro
    except serpapi.ErroSerpApi as erro:
        raise BuscaInstagramIndisponivel(f"SerpApi: {erro}") from erro
    try:
        perfil, posts = serpapi.normalizar_perfil_instagram(resposta_api)
    except Exception as erro:
        logger.exception("Falha ao normalizar perfil do Instagram para %s", handle)
        raise BuscaInstagramIndisponivel(
            f"Resposta inesperada da SerpApi para @{handle}: {erro}"
        ) from erro
    return {"perfil": perfil, "posts": posts}


def montar_posts(handle: str, posts: list[dict]) -> list[dict]:
    """Postagens com shortcode, URL, miniatura e pHash (baixa as miniaturas)."""
    if not posts:
        raise BuscaInstagramIndisponivel(
            f"A SerpApi respondeu, mas nao trouxe postagens publicas de @{handle}."
        )

    resultado = []
    for post in posts:
        miniatura = post.get("miniatura") or ""
        phash = baixar_hash_da_miniatura(miniatura)
        if not phash:
            miniatura = post.get("miniatura_alternativa") or ""
            phash = baixar_hash_da_miniatura(miniatura)
        if not phash:
            continue
        shortcode = sites_oficiais.shortcode_instagram(post.get("link", ""))
        if not shortcode:
            continue
        resultado.append(
            {
                "shortcode": shortcode,
                "url": post.get("link", ""),
                "miniatura": miniatura,
                "phash": phash,
            }
        )
    if not resultado:
        raise BuscaInstagramIndisponivel(
            f"A SerpApi encontrou {len(posts)} postagem(ns) de @{handle}, "
            "mas nenhuma miniatura pode ser baixada para calcular o hash."
        )
    return resultado


def buscar_postagens(handle: str) -> tuple[dict, list[dict]]:
    """Perfil e postagens com shortcode, URL, miniatura e pHash."""
    handle = (handle or "").strip().lstrip("@")
    if not handle:
        raise BuscaInstagramIndisponivel(
            "Esta criadora nao tem o @ do Instagram cadastrado no perfil de busca. "
            "Preencha o perfil de busca para capturar as postagens."
        )
    dados = buscar_perfil(handle)
    return dados["perfil"], montar_posts(handle, dados["posts"])
