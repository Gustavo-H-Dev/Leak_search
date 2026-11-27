import re

TERMOS_PADRAO = ("vazado", "leaked", "naked", "nude")
SITES_PADRAO = ("erome.com",)
MAXIMO_TERMOS = 15
MAXIMO_SITES = 20
SITES_POR_CONSULTA = 5
MAXIMO_CONSULTAS_SITES = 2
PERIODOS = {
    "": "Qualquer data",
    "d": "Último dia",
    "w": "Última semana",
    "m": "Último mês",
    "y": "Último ano",
}
_DOMINIO = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+")


def _linhas(texto: str) -> list[str]:
    return [parte.strip() for parte in re.split(r"[\n,;]", texto or "")]


def limpar_termos(texto: str) -> list[str]:
    termos = []
    for item in _linhas(texto):
        item = re.sub(r'["()]', "", item).strip()
        if item and len(item) <= 40 and item.lower() not in {t.lower() for t in termos}:
            termos.append(item)
    return termos[:MAXIMO_TERMOS]


def limpar_sites(texto: str) -> list[str]:
    sites = []
    for item in _linhas(texto):
        dominio = re.sub(r"^[a-z]+://", "", item.lower()).split("/")[0]
        dominio = dominio.removeprefix("www.")
        if _DOMINIO.fullmatch(dominio) and dominio not in sites:
            sites.append(dominio)
    return sites[:MAXIMO_SITES]


def gerar_consultas_busca(arrobas, termos, sites, limite: int = 12) -> list[str]:
    """Combina cada arroba com termos e com `site:` dos dominios configurados."""
    arrobas = [a.strip().lstrip("@") for a in arrobas if a and a.strip()][:10]
    consultas: list[str] = []
    vistos: set[str] = set()

    def adicionar(consulta: str) -> None:
        consulta = " ".join(consulta.split())
        chave = consulta.lower()
        if consulta and chave not in vistos and len(consultas) < max(1, limite):
            vistos.add(chave)
            consultas.append(consulta)

    for termo in termos:
        termo = termo.strip()
        if not termo:
            continue
        for arroba in arrobas:
            adicionar(f'"@{arroba}" "{termo}"')
    for site in sites:
        for arroba in arrobas:
            adicionar(f'"@{arroba}" site:{site}')
    return consultas


def montar_consultas(nome: str, handle: str, termos, sites) -> list[str]:
    """Agrupa tudo em poucas consultas: uma de termos e até duas de sites."""
    nome = nome.replace('"', "").strip()
    handle = handle.replace('"', "").strip().removeprefix("@")
    base = f'("{nome}" OR "@{handle}")'
    consultas = []
    if termos:
        grupo = " OR ".join(f'"{t}"' if " " in t else t for t in termos)
        consultas.append(f"{base} ({grupo})")
    limite = SITES_POR_CONSULTA * MAXIMO_CONSULTAS_SITES
    sites = list(sites)[:limite]
    for inicio in range(0, len(sites), SITES_POR_CONSULTA):
        grupo = " OR ".join(f"site:{s}" for s in sites[inicio : inicio + SITES_POR_CONSULTA])
        consultas.append(f"{base} ({grupo})")
    return consultas
