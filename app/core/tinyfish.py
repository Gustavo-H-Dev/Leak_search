"""Cliente TinyFish (TinySearch) para busca web por texto."""

import os
import threading
import time

import httpx

API_URL = "https://api.search.tinyfish.ai"
TIMEOUT = 30
LOCATION = "BR"
LANGUAGE = "pt"
TENTATIVAS = 3


class ErroTinyFish(Exception):
    pass


class TinyFishNaoConfigurada(ErroTinyFish):
    pass


class TinyFishSemCreditos(ErroTinyFish):
    pass


def _limite_por_minuto() -> int:
    try:
        return max(1, int(os.getenv("TINYFISH_REQUISICOES_POR_MINUTO", "30")))
    except ValueError:
        return 30


def _chave() -> str:
    chave = os.getenv("TINYFISH_API_KEY", "").strip()
    if not chave:
        raise TinyFishNaoConfigurada(
            "Configure a variável TINYFISH_API_KEY no ambiente do servidor."
        )
    return chave


_controle = threading.Lock()
_ultima_requisicao = 0.0


def _aguardar_janela() -> None:
    """Mantem no maximo o limite por minuto, sem rajadas."""
    global _ultima_requisicao
    intervalo = 60.0 / _limite_por_minuto()
    with _controle:
        agora = time.monotonic()
        espera = _ultima_requisicao + intervalo - agora
        if espera > 0:
            time.sleep(espera)
            agora = time.monotonic()
        _ultima_requisicao = agora


def _mensagem_de_erro(resposta: httpx.Response) -> str:
    try:
        dados = resposta.json()
    except ValueError:
        return f"TinyFish respondeu HTTP {resposta.status_code}."
    if isinstance(dados, dict):
        detalhe = dados.get("error") or dados.get("message") or dados.get("detail")
        codigo = dados.get("code")
        if detalhe or codigo:
            return f"TinyFish: {detalhe or codigo}."
    return f"TinyFish respondeu HTTP {resposta.status_code}."


def pesquisar(query: str) -> dict:
    """Executa uma busca e devolve o JSON bruto da resposta."""
    chave = _chave()
    if not (query or "").strip():
        raise ErroTinyFish("A consulta do TinyFish está vazia.")

    ultima_falha = ""
    for tentativa in range(TENTATIVAS):
        _aguardar_janela()
        try:
            resposta = httpx.get(
                API_URL,
                params={
                    "query": query.strip(),
                    "location": LOCATION,
                    "language": LANGUAGE,
                },
                headers={"X-API-Key": chave},
                timeout=TIMEOUT,
            )
        except httpx.RequestError as erro:
            ultima_falha = "Não foi possível conectar ao TinyFish."
            if tentativa + 1 >= TENTATIVAS:
                raise ErroTinyFish(ultima_falha) from erro
            time.sleep(2**tentativa)
            continue

        if resposta.status_code == 429:
            espera = resposta.headers.get("retry-after", "")
            try:
                pausa = float(espera)
            except ValueError:
                pausa = 60.0 / _limite_por_minuto() * (tentativa + 2)
            ultima_falha = "TinyFish: limite de requisições atingido (HTTP 429)."
            if tentativa + 1 >= TENTATIVAS:
                raise ErroTinyFish(ultima_falha)
            time.sleep(max(1.0, pausa))
            continue

        if resposta.status_code == 402:
            raise TinyFishSemCreditos(
                "O TinyFish chegou ao limite gratuito do dia e a carteira está "
                "sem saldo. Adicione créditos em agent.tinyfish.ai/wallet ou "
                "aguarde a renovação diária (00:00 UTC)."
            )

        if resposta.status_code in {500, 503}:
            ultima_falha = _mensagem_de_erro(resposta)
            if tentativa + 1 >= TENTATIVAS:
                raise ErroTinyFish(ultima_falha)
            time.sleep(2**tentativa * 2)
            continue

        if resposta.status_code >= 400:
            raise ErroTinyFish(_mensagem_de_erro(resposta))

        try:
            dados = resposta.json()
        except ValueError as erro:
            raise ErroTinyFish("O TinyFish retornou uma resposta inválida.") from erro
        if not isinstance(dados, dict):
            raise ErroTinyFish("O TinyFish retornou uma resposta inválida.")
        return dados

    raise ErroTinyFish(ultima_falha or "O TinyFish não concluiu a busca.")
