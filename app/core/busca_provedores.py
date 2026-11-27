"""Interface unica dos provedores de busca por texto."""

from dataclasses import dataclass, field

from app.core import pontuacao, serpapi, tinyfish

PROVEDORES_TEXTO = {"serpapi", "tinyfish"}
PROVEDORES_NOMES = {
    "serpapi": "SerpAPI (Google)",
    "tinyfish": "TinyFish (web)",
}


@dataclass(frozen=True)
class Candidato:
    url: str
    dominio: str
    titulo: str
    trecho: str
    miniatura_url: str = ""
    provedor: str = ""
    resposta_bruta: dict = field(default_factory=dict)


def _candidato(provedor, url, titulo, trecho, miniatura, bruto):
    normalizada = pontuacao.normalizar_url(url)
    if not normalizada:
        return None
    return Candidato(
        url=normalizada,
        dominio=pontuacao.dominio_de(normalizada),
        titulo=(titulo or "Resultado")[:500],
        trecho=(trecho or "")[:4000],
        miniatura_url=miniatura or "",
        provedor=provedor,
        resposta_bruta=bruto,
    )


def buscar_serpapi(
    query: str, engine: str = "google_images_light", parametros: dict | None = None
) -> list[Candidato]:
    dados = serpapi.pesquisar(
        engine, q=query, gl="br", hl="pt", safe="off", **(parametros or {})
    )
    candidatos = []
    for item in serpapi.normalizar_resultados(dados, engine):
        candidato = _candidato(
            "serpapi",
            item.get("link", ""),
            item.get("titulo", ""),
            item.get("resumo", ""),
            item.get("miniatura", ""),
            {"engine": engine, "query": query, "resultado": item},
        )
        if candidato:
            candidatos.append(candidato)
    return candidatos


def buscar_tinyfish(query: str) -> list[Candidato]:
    dados = tinyfish.pesquisar(query)
    candidatos = []
    resultados = dados.get("results")
    if not isinstance(resultados, list):
        return candidatos
    for item in resultados:
        if not isinstance(item, dict):
            continue
        candidato = _candidato(
            "tinyfish",
            item.get("url", ""),
            item.get("title", ""),
            item.get("snippet", ""),
            "",
            {"query": query, "resultado": item},
        )
        if candidato:
            candidatos.append(candidato)
    return candidatos


def buscar(provedor: str, query: str, **opcoes) -> list[Candidato]:
    if provedor == "tinyfish":
        return buscar_tinyfish(query)
    if provedor == "serpapi":
        return buscar_serpapi(
            query, engine=opcoes.get("engine", "google_images_light"), parametros=opcoes.get("parametros")
        )
    raise ValueError(f"Provedor de busca desconhecido: {provedor}")


def configurado(provedor: str) -> bool:
    import os

    variavel = {
        "serpapi": "SERPAPI_API_KEY",
        "tinyfish": "TINYFISH_API_KEY",
    }.get(provedor, "")
    return bool(variavel and os.getenv(variavel, "").strip())
