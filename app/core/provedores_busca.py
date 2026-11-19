import os


PROVEDORES = (
    {
        "id": "google_vision",
        "nome": "Google Cloud Vision",
        "tipo": "imagem",
        "descricao": "Web Detection para páginas e imagens correspondentes.",
        "variaveis": ("GOOGLE_APPLICATION_CREDENTIALS",),
        "integrado": True,
    },
    {
        "id": "serpapi",
        "nome": "SerpApi",
        "tipo": "texto/imagem",
        "descricao": "Google Images, Lens e Instagram por meio do SerpApi.",
        "variaveis": ("SERPAPI_API_KEY",),
        "integrado": True,
    },
    {
        "id": "brave",
        "nome": "Brave Search",
        "tipo": "texto",
        "descricao": "Busca textual por nome e arroba.",
        "variaveis": ("BRAVE_SEARCH_API_KEY",),
        "integrado": False,
    },
    {
        "id": "tineye",
        "nome": "TinEye",
        "tipo": "imagem",
        "descricao": "Busca especializada por cópias de uma imagem.",
        "variaveis": ("TINEYE_API_KEY",),
        "integrado": False,
    },
    {
        "id": "dataforseo",
        "nome": "DataForSEO",
        "tipo": "texto",
        "descricao": "Resultados Google Images e pesquisa orgânica.",
        "variaveis": ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
        "integrado": False,
    },
    {
        "id": "searxng",
        "nome": "SearXNG",
        "tipo": "texto",
        "descricao": "Metabuscador próprio com vários mecanismos.",
        "variaveis": ("SEARXNG_BASE_URL",),
        "integrado": False,
    },
)


def listar_provedores(tipo: str = "") -> list[dict]:
    provedores = []
    for provedor in PROVEDORES:
        if tipo and tipo not in provedor["tipo"]:
            continue
        configurado = all(
            os.getenv(variavel, "").strip()
            for variavel in provedor["variaveis"]
        )
        item = dict(provedor)
        item["configurado"] = configurado if provedor["integrado"] else False
        item["status"] = (
            "Ativo" if item["configurado"]
            else "Integrado, não configurado" if provedor["integrado"]
            else "Disponível para integração"
        )
        provedores.append(item)
    return provedores


def obter_provedor(provedor_id: str) -> dict | None:
    for provedor in listar_provedores():
        if provedor["id"] == provedor_id:
            return provedor
    return None
