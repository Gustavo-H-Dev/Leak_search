import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import httpx

from app.core import imagens

TAMANHO_MAXIMO_IMAGEM = 5 * 1024 * 1024
MAXIMO_REDIRECIONAMENTOS = 3
TIMEOUT = 10
MAXIMO_HASHES_POR_BUSCA = 60


def _destino_publico(url: str) -> bool:
    try:
        partes = urlsplit(url)
        if partes.scheme not in {"http", "https"} or not partes.hostname:
            return False
        enderecos = socket.getaddrinfo(
            partes.hostname, partes.port or 443, type=socket.SOCK_STREAM
        )
    except (OSError, ValueError):
        return False
    return bool(enderecos) and all(
        ipaddress.ip_address(endereco[4][0]).is_global for endereco in enderecos
    )


def hash_da_imagem_remota(url: str) -> str:
    """Baixa a imagem em memória, devolve só o pHash e descarta os bytes.

    Retorna "" se o destino não for público, não for imagem ou falhar.
    """
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as cliente:
            for _ in range(MAXIMO_REDIRECIONAMENTOS + 1):
                if not _destino_publico(url):
                    return ""
                with cliente.stream("GET", url) as resposta:
                    if resposta.is_redirect:
                        url = urljoin(url, resposta.headers.get("location", ""))
                        continue
                    if resposta.status_code != 200:
                        return ""
                    tipo = resposta.headers.get("content-type", "")
                    if not tipo.lower().startswith("image/"):
                        return ""
                    dados = bytearray()
                    for bloco in resposta.iter_bytes():
                        dados.extend(bloco)
                        if len(dados) > TAMANHO_MAXIMO_IMAGEM:
                            return ""
                    return imagens.calcular_phash(bytes(dados))
    except (httpx.HTTPError, ValueError, OSError):
        return ""
    return ""


def calcular_hashes(urls: list[str]) -> dict[str, str]:
    urls = list(dict.fromkeys(urls))[:MAXIMO_HASHES_POR_BUSCA]
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        return dict(zip(urls, executor.map(hash_da_imagem_remota, urls)))


def melhor_referencia(phash: str, referencias: list[dict]) -> dict | None:
    """Referência de menor distância perceptual; None sem hash ou referências."""
    if not phash:
        return None
    melhor = None
    for referencia in referencias:
        dist = imagens.distancia(phash, referencia["phash"])
        if melhor is None or dist < melhor["distancia"]:
            melhor = {**referencia, "distancia": dist}
    return melhor


def ordenar_por_atencao(candidatos: list[dict]) -> list[dict]:
    ordem = {nivel: posicao for posicao, nivel in enumerate(imagens.NIVEIS)}
    return sorted(
        candidatos,
        key=lambda c: (
            ordem[c["nivel"]],
            c["distancia"] if c["distancia"] is not None else 999,
        ),
    )
