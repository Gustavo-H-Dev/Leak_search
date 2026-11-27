import os
import re

from app.core import hosts_busca, variantes_nome
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
PALAVRAS_NOME_BASE = ("fotos", "videos", "album", "solo")
MAXIMO_TERMOS = 15
MAXIMO_SITES = 20
MAXIMO_MODELOS_M1 = 4
MAXIMO_ARROBAS_CONSULTA = 10
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


def _arrobas_limpos(arrobas) -> list[str]:
    """Mantem a grafia informada (M1 depende disso) e remove duplicatas."""
    limpos = []
    for arroba in arrobas or ():
        valor = " ".join(str(arroba or "").split()).lstrip("@")
        if valor and valor.lower() not in {a.lower() for a in limpos}:
            limpos.append(valor)
    return limpos[:MAXIMO_ARROBAS_CONSULTA]


def _site_ativo(site_ativo) -> bool:
    if site_ativo is not None:
        return bool(site_ativo)
    return os.getenv("CONSULTAS_SITE_ATIVAS", "0").strip().lower() in {
        "1",
        "true",
        "on",
        "sim",
    }


def gerar_consultas_priorizadas(
    arrobas,
    termos,
    sites,
    limite: int = 18,
    nome: str = "",
    *,
    hosts=None,
    site_ativo=None,
) -> list[dict]:
    """Consultas em modelos, na ordem de prioridade M1, M2, M3, M4, M5.

    Cada item traz `modelo`, `consulta` e `parametros` (o que a consulta usou),
    para o log registrar qual modelo achou cada candidato.

    M1  todos os arrobas conhecidos com OR + termo de vazamento
    M2  {nome_base} videos {host} para hospedagem/indice aprovados
    M3  {nome_base} {fotos|videos|album|solo}
    M4  {nome_base} site:{dominio} (experimental, desligado por padrao)
    M5  consultas complementares ja usadas antes (@, termos, nome publico)
    """
    limite = max(1, int(limite))
    arrobas = _arrobas_limpos(arrobas)
    termos_limpos = []
    for termo in termos or ():
        termo = " ".join(str(termo or "").split())
        if termo and termo.lower() not in {t.lower() for t in termos_limpos}:
            termos_limpos.append(termo)
    nome = " ".join((nome or "").split())

    base = variantes_nome.nome_base(arrobas)
    hosts_aprovados = (
        [dict(item) for item in hosts]
        if hosts is not None
        else hosts_busca.hosts(hosts_busca.TIPOS_CONSULTA)
    )

    planos: list[dict] = []
    vistos: set[str] = set()

    def adicionar(modelo: str, consulta: str, parametros: dict | None = None) -> None:
        consulta = " ".join(consulta.split())
        chave = consulta.lower()
        if not consulta or chave in vistos or len(planos) >= limite:
            return
        vistos.add(chave)
        planos.append(
            {
                "modelo": modelo,
                "consulta": consulta,
                "parametros": parametros or {},
            }
        )

    # M1: o arroba completo citado por inteiro costuma ser o unico jeito de
    # achar paginas que usam o @ como titulo. O OR junta as grafias conhecidas.
    if arrobas:
        grupo = " OR ".join(arrobas)
        for termo in termos_limpos[:MAXIMO_MODELOS_M1]:
            adicionar(
                "M1",
                f"{grupo} {termo}",
                {"arrobas": list(arrobas), "termo": termo},
            )

    # M2: o titulo do album costuma usar o nome-base, e o indice do host e
    # encontravel pelo nome do host ("bunkr") junto de "videos".
    if base:
        for host in hosts_aprovados:
            termo_host = (host.get("termo") or "").strip()
            if not termo_host:
                continue
            adicionar(
                "M2",
                f"{base} videos {termo_host}",
                {
                    "nome_base": base,
                    "host": host.get("host", ""),
                    "host_tipo": host.get("tipo", ""),
                },
            )

    # M3: variacoes simples do tipo de conteudo do album.
    if base:
        for palavra in PALAVRAS_NOME_BASE:
            adicionar(
                "M3",
                f"{base} {palavra}",
                {"nome_base": base, "termo": palavra},
            )

    # M4: operador site: e experimental no TinyFish; so entra com a flag.
    if base and _site_ativo(site_ativo):
        for host in hosts_aprovados:
            adicionar(
                "M4",
                f"{base} site:{host.get('host', '')}",
                {
                    "nome_base": base,
                    "host": host.get("host", ""),
                    "experimental": True,
                },
            )

    # M5: rede de seguranca com as consultas genericas que ja existiam.
    _adicionar_complementares(adicionar, arrobas, termos_limpos, sites, nome)

    return planos


def _adicionar_complementares(adicionar, arrobas, termos, sites, nome) -> None:
    """Formas genericas: @ com termos, @ sozinho, plataformas, nome e sites."""
    teto_termos = max(1, len(termos))
    for termo in termos[:teto_termos]:
        for arroba in arrobas:
            adicionar(
                "M5",
                f'"@{arroba}" "{termo}"',
                {"arroba": arroba, "termo": termo, "forma": "arroba_termo"},
            )

    for arroba in arrobas:
        adicionar(
            "M5", f'"@{arroba}"', {"arroba": arroba, "forma": "arroba_sozinho"}
        )

    grupo_plataformas = " OR ".join(PLATAFORMAS_PADRAO)
    for arroba in arrobas:
        adicionar(
            "M5",
            f'"@{arroba}" ({grupo_plataformas})',
            {"arroba": arroba, "forma": "arroba_plataformas"},
        )

    nome_limpo = re.sub(r'["()]', "", nome or "").strip()
    if nome_limpo:
        adicionar("M5", f'"{nome_limpo}"', {"forma": "nome"})
        adicionar("M5", f'"{nome_limpo}" vazado', {"forma": "nome_vazado"})
        for arroba in arrobas[:2]:
            adicionar(
                "M5",
                f'"{nome_limpo}" "@{arroba}"',
                {"arroba": arroba, "forma": "nome_arroba"},
            )

    for site in _sites_para_consulta(sites):
        for arroba in arrobas:
            adicionar(
                "M5",
                f'"@{arroba}" site:{site}',
                {"arroba": arroba, "site": site, "forma": "site"},
            )


def gerar_consultas_busca(arrobas, termos, sites, limite: int = 18, nome: str = ""):
    """Lista de consultas (texto) na ordem priorizada dos modelos."""
    return [
        plano["consulta"]
        for plano in gerar_consultas_priorizadas(
            arrobas, termos, sites, limite, nome=nome
        )
    ]


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
