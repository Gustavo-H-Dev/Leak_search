import re

from app.core.dominios_vazamento import DOMINIOS_VAZAMENTO_PADRAO

TERMOS_PADRAO = ("vazado", "vazada", "leaked", "nude", "nudes", "exposed")
SITES_PADRAO = (
    "erome.com",
    "fapello.com",
    "thothub.tv",
    "coomer.su",
    "kemono.su",
)
# Plataformas de conteudo que costumam aparecer nas consultas genericas.
PLATAFORMAS_PADRAO = ("privacy", "onlyfans", "telegram", "pack", "xvideos")
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


def _sites_para_consulta(sites) -> list[str]:
    """Configurados primeiro, completados com os dominios conhecidos."""
    todos = [s for s in sites if s]
    for padrao in SITES_PADRAO:
        if padrao not in todos:
            todos.append(padrao)
    return todos


def gerar_consultas_busca(
    arrobas, termos, sites, limite: int = 18, nome: str = ""
) -> list[str]:
    """Consultas diversificadas: arroba+termo, arroba sozinha, nome e sites.

    A ideia e cobrir "todas as formas": o @ com palavras de vazamento, o @ puro
    (para achar paginas que so citam o perfil), o @ combinado com plataformas,
    o nome publico e, por fim, os dominios de vazamento.
    """
    arrobas = [a.strip().lstrip("@") for a in arrobas if a and a.strip()][:10]
    limite = max(1, int(limite))
    consultas: list[str] = []
    vistos: set[str] = set()

    def adicionar(consulta: str) -> None:
        consulta = " ".join(consulta.split())
        chave = consulta.lower()
        if consulta and chave not in vistos and len(consultas) < limite:
            vistos.add(chave)
            consultas.append(consulta)

    # 1. Cada arroba com cada termo, mas sem consumir o orcamento inteiro:
    #    sobra espaco para as consultas genericas.
    teto_termos = max(1, (limite + 1) // 2)
    for termo in termos:
        termo = " ".join(str(termo).split())
        if not termo:
            continue
        for arroba in arrobas:
            if len(consultas) >= teto_termos:
                break
            adicionar(f'"@{arroba}" "{termo}"')

    # 2. O @ sozinho: encontra paginas que apenas citam o perfil.
    for arroba in arrobas:
        adicionar(f'"@{arroba}"')

    # 3. O @ com plataformas onde conteudo costuma ser redistribuido.
    grupo_plataformas = " OR ".join(PLATAFORMAS_PADRAO)
    for arroba in arrobas:
        adicionar(f'"@{arroba}" ({grupo_plataformas})')

    # 4. O nome publico, sozinho e junto do @.
    nome_limpo = re.sub(r'["()]', "", nome or "").strip()
    if nome_limpo:
        adicionar(f'"{nome_limpo}"')
        adicionar(f'"{nome_limpo}" vazado')
        for arroba in arrobas[:2]:
            adicionar(f'"{nome_limpo}" "@{arroba}"')

    # 5. Dominios: os configurados e os conhecidos de vazamento.
    for site in _sites_para_consulta(sites):
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
