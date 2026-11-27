"""Dominios suspeitos: sinais mensuraveis e revisao humana.

A lista deixou de ser manual. O fluxo de busca registra quais dominios
apareceram em cada execucao; aqui medimos sinais objetivos (nome da criadora,
termos de vazamento, padrao de hospedagem de arquivos) e, quando o dominio
aparece em execucoes/criadoras suficientes, ele vira `sugerido` para revisao.

Nada aqui visita paginas nem baixa midia: os sinais usam somente o titulo, o
trecho e a URL que o provedor de busca ja devolveu. Nenhum dominio e aprovado
automaticamente -- so um humano distingue um site de vazamento de uma empresa
qualquer (ex.: bunkr.net).
"""

import logging
import os
import re
from collections import defaultdict
from pathlib import Path

from app.core import hosts_busca, pontuacao

logger = logging.getLogger(__name__)

CAMINHO_SEED = Path(__file__).with_name("dominios_seed.yaml")

TIPOS = ("hospedagem_arquivos", "indice_albuns", "agregador_vazamento", "outro")
STATUS = ("sugerido", "aprovado", "rejeitado")
TIPOS_HOSPEDAGEM = ("hospedagem_arquivos", "indice_albuns")

# Plataformas que nunca devem ser sugeridas (somadas ao arquivo de seed).
IGNORADOS_PADRAO = (
    "instagram.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "youtube.com",
    "google.com",
    "wikipedia.org",
    "github.com",
)

TERMOS_VAZAMENTO_PADRAO = (
    "vazado",
    "vazada",
    "leak",
    "leaked",
    "nude",
    "nudes",
    "onlyfans leak",
    "free download",
    "full videos",
)

# Caminhos de album/arquivo e trechos que anunciam hospedagem de arquivos.
PADROES_HOSPEDAGEM_PADRAO = (
    r"/a/[A-Za-z0-9]+",
    r"/album/",
    r"/albums/",
    r"/f/[A-Za-z0-9]+",
    r"\b(?:files?|mb|gb)\b",
)

PESO_NOME = 40
PESO_TERMO = 10
PESO_TERMOS_MAXIMO = 30
PESO_HOSPEDAGEM = 25
LIMIAR_PADRAO = 50
MINIMO_EXECUCOES_PADRAO = 2
MINIMO_CRIADORAS_PADRAO = 2
LIMITE_REVISOES_PADRAO = 10

_TERMO_SEPARADOR = re.compile(r"[\n,;]")


def _env_int(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, str(padrao)).strip())
    except (TypeError, ValueError):
        return padrao


def limiar() -> int:
    return _env_int("DOMINIOS_LIMIAR_SUGESTAO", LIMIAR_PADRAO)


def minimo_execucoes() -> int:
    return max(1, _env_int("DOMINIOS_MIN_EXECUCOES", MINIMO_EXECUCOES_PADRAO))


def minimo_criadoras() -> int:
    return max(1, _env_int("DOMINIOS_MIN_CRIADORAS", MINIMO_CRIADORAS_PADRAO))


def limite_revisoes() -> int:
    return max(1, _env_int("DOMINIOS_LIMITE_REVISOES", LIMITE_REVISOES_PADRAO))


def _lista_env(nome: str) -> list[str]:
    return [
        item.strip()
        for item in _TERMO_SEPARADOR.split(os.getenv(nome, "") or "")
        if item.strip()
    ]


def termos_vazamento() -> list[str]:
    extras = _lista_env("DOMINIOS_TERMOS_VAZAMENTO")
    termos = [termo.lower() for termo in (*TERMOS_VAZAMENTO_PADRAO, *extras)]
    return list(dict.fromkeys(termos))


def padroes_hospedagem() -> list[re.Pattern]:
    padroes = list(PADROES_HOSPEDAGEM_PADRAO) + _lista_env("DOMINIOS_PADROES_HOSPEDAGEM")
    compilados = []
    for padrao in padroes:
        try:
            compilados.append(re.compile(padrao, re.IGNORECASE))
        except re.error as erro:
            logger.warning("Padrao de hospedagem invalido %s: %s", padrao, erro)
    return compilados


def normalizar_dominio(valor: str) -> str:
    """Dominio minusculo, sem "www." e sem caminho."""
    dominio = (valor or "").strip().lower()
    dominio = re.sub(r"^[a-z]+://", "", dominio)
    dominio = dominio.split("/")[0].split("?")[0].split("#")[0]
    dominio = dominio.split("@")[-1].split(":")[0]
    return dominio.removeprefix("www.").rstrip(".")


def _arquivo_seed() -> Path:
    caminho = os.getenv("DOMINIOS_SEED_ARQUIVO", "").strip()
    return Path(caminho) if caminho else CAMINHO_SEED


def carregar_seed() -> tuple[list[dict], list[str]]:
    """Le `dominios_seed.yaml` e devolve (dominios, ignorados).

    Arquivo ausente ou invalido nao derruba a aplicacao: apenas nao semeia.
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML nao instalado; seed de dominios ignorado")
        return [], []
    try:
        dados = yaml.safe_load(_arquivo_seed().read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as erro:
        logger.warning("Nao foi possivel ler o seed de dominios: %s", erro)
        return [], []

    itens = []
    for entrada in dados.get("seed") or ():
        if not isinstance(entrada, dict):
            continue
        dominio = normalizar_dominio(entrada.get("dominio") or "")
        if not dominio:
            continue
        tipo = (entrada.get("tipo") or "outro").strip().lower()
        status = (entrada.get("status") or "sugerido").strip().lower()
        itens.append(
            {
                "dominio": dominio,
                "tipo": tipo if tipo in TIPOS else "outro",
                "status": status if status in STATUS else "sugerido",
            }
        )
    ignorados = [
        normalizar_dominio(item)
        for item in dados.get("ignorados") or ()
        if normalizar_dominio(item)
    ]
    return itens, ignorados


def dominios_ignorados() -> set[str]:
    """Lista base + seed + configuracao avulsa (DOMINIOS_IGNORADOS)."""
    _itens, do_arquivo = carregar_seed()
    todos = [
        *IGNORADOS_PADRAO,
        *do_arquivo,
        *_lista_env("DOMINIOS_IGNORADOS"),
    ]
    return {normalizar_dominio(item) for item in todos if normalizar_dominio(item)}


def garantir_seed() -> None:
    """Aplica o seed e a lista de ignorados no banco (idempotente)."""
    from app import db

    itens, do_arquivo = carregar_seed()
    ignorados = [*IGNORADOS_PADRAO, *do_arquivo, *_lista_env("DOMINIOS_IGNORADOS")]
    db.semear_dominios(itens, ignorados)


def _contem(texto: str, nome: str) -> bool:
    nome = " ".join((nome or "").split()).lower()
    if len(nome) < 4 or not texto:
        return False
    return nome in texto.lower()


def sinais_do_dominio(
    candidatos, arrobas=(), variantes=(), nome_base: str = ""
) -> dict:
    """Nota e motivos de UM dominio a partir dos candidatos que o citaram.

    Funcao pura (sem banco e sem rede) para poder ser testada isoladamente.
    """
    nota = 0
    sinais = []

    nomes = {
        " ".join(str(nome).split()).lower()
        for nome in (*arrobas, *variantes, nome_base)
        if str(nome or "").strip()
    }
    nomes = {nome for nome in nomes if len(nome) >= 4}

    achado = ""
    for candidato in candidatos:
        texto = f"{candidato.get('titulo') or ''} {candidato.get('url') or ''}"
        for nome in sorted(nomes, key=len, reverse=True):
            if _contem(texto, nome):
                achado = nome
                break
        if achado:
            break
    if achado:
        nota += PESO_NOME
        sinais.append(f'nome da criadora "{achado}" no título ou na URL')

    termos = termos_vazamento()
    encontrados = []
    for candidato in candidatos:
        texto = f"{candidato.get('titulo') or ''} {candidato.get('trecho') or ''}"
        for termo in termos:
            if termo in texto.lower() and termo not in encontrados:
                encontrados.append(termo)
    if encontrados:
        nota += min(PESO_TERMOS_MAXIMO, PESO_TERMO * len(encontrados))
        sinais.append("termos de vazamento: " + ", ".join(encontrados[:5]))

    padroes = padroes_hospedagem()
    hospedagem = ""
    for candidato in candidatos:
        url = candidato.get("url") or ""
        try:
            caminho = pontuacao.normalizar_url(url).split("?")[0]
        except ValueError:
            caminho = url
        texto = f"{candidato.get('titulo') or ''} {candidato.get('trecho') or ''}"
        for padrao in padroes:
            if padrao.search(caminho) or padrao.search(texto):
                hospedagem = padrao.pattern
                break
        if hospedagem:
            break
    if hospedagem:
        nota += PESO_HOSPEDAGEM
        sinais.append(f"padrão de hospedagem de arquivos ({hospedagem})")

    return {"nota": nota, "sinais": sinais}


def contexto_aprovado() -> dict:
    """Dominios aprovados para pontuacao e hosts aprovados para as consultas.

    Nunca levanta excecao: sem banco (ou sem as tabelas) devolve contexto vazio
    e a pontuacao segue apenas com o que a criadora configurou.
    """
    from app import db

    try:
        aprovados = db.dominios_aprovados()
    except Exception:
        logger.debug("Sem lista de dominios aprovados; seguindo sem ela")
        return {"vazamento": (), "tipos": {}, "hosts": ()}
    return {
        "vazamento": tuple(item["dominio"] for item in aprovados),
        "tipos": {
            item["dominio"]: item["tipo"]
            for item in aprovados
            if item["tipo"] in TIPOS_HOSPEDAGEM
        },
        "hosts": tuple(aprovados),
    }


def hosts_para_consulta() -> list[dict]:
    """Hosts aprovados dos tipos de arquivos/índice para os modelos M2/M4.

    Os hosts já configurados em `hosts_busca.json` continuam valendo (eles
    trazem o termo de consulta e o padrão do ID do álbum); a lista do banco
    acrescenta os novos aprovados e remove os rejeitados.
    """
    from app import db

    aprovados = contexto_aprovado()["hosts"]
    aprovados_hosts = {
        item["dominio"]: item
        for item in aprovados
        if item["tipo"] in TIPOS_HOSPEDAGEM
    }
    try:
        rejeitados = {
            normalizar_dominio(item["dominio"])
            for item in db.listar_dominios_suspeitos("rejeitado")
        }
    except Exception:
        rejeitados = set()

    resultado = []
    vistos = set()

    def adicionar(host, tipo, termo, padroes):
        host = normalizar_dominio(host)
        if not host or host in vistos or host in rejeitados:
            return
        vistos.add(host)
        resultado.append(
            {"host": host, "tipo": tipo, "termo": termo, "padroes": tuple(padroes)}
        )

    for item in aprovados_hosts.values():
        conhecido = next(
            (h for h in hosts_busca.hosts() if h["host"] == item["dominio"]), {}
        )
        adicionar(
            item["dominio"],
            item["tipo"],
            conhecido.get("termo") or item["dominio"].split(".")[0],
            conhecido.get("padroes") or (),
        )
    for item in hosts_busca.hosts(hosts_busca.TIPOS_CONSULTA):
        adicionar(item["host"], item["tipo"], item["termo"], item["padroes"])
    return resultado


def avaliar_dominios(
    execucao_id: int, *, arrobas=(), variantes=(), nome_base: str = ""
) -> list[dict]:
    """Registra os sinais dos domínios de uma execução e sugere os maduros.

    Regras: nada de domínio ignorado, já aprovado ou já rejeitado; a sugestão
    exige sinais acima do limite E aparição em pelo menos 2 execuções distintas
    (ou 2 criadoras distintas). Nunca grava `aprovado`.
    """
    from app import db

    execucao = db.obter_busca_execucao(execucao_id)
    if not execucao:
        return []
    criadora_id = execucao["criadora_id"]
    candidatos = db.listar_candidatos_da_execucao(execucao_id)
    if not candidatos:
        return []

    por_dominio: dict[str, list[dict]] = defaultdict(list)
    for candidato in candidatos:
        dominio = normalizar_dominio(
            candidato.get("dominio") or pontuacao.dominio_de(candidato.get("url") or "")
        )
        if dominio:
            por_dominio[dominio].append(dict(candidato))

    ignorados = dominios_ignorados()
    try:
        ignorados |= set(db.listar_dominios_ignorados())
    except Exception:
        pass
    conhecidos = {
        item["dominio"]: item["status"] for item in db.listar_dominios_suspeitos()
    }
    familias = {
        item["dominio"].split(".")[0]: item["dominio"]
        for item in db.dominios_aprovados()
    }

    sugestoes = []
    for dominio, lista in sorted(por_dominio.items()):
        if dominio in ignorados or conhecidos.get(dominio) in {"aprovado", "rejeitado"}:
            continue
        avaliacao = sinais_do_dominio(
            lista, arrobas=arrobas, variantes=variantes, nome_base=nome_base
        )
        if avaliacao["nota"] < limiar():
            continue

        db.registrar_execucao_dominio(
            dominio, execucao_id, criadora_id, len(lista), avaliacao["nota"]
        )
        contagens = db.contagens_do_dominio(dominio)
        if not (
            contagens["execucoes"] >= minimo_execucoes()
            or contagens["criadoras"] >= minimo_criadoras()
        ):
            continue

        observacao = ""
        familia = familias.get(dominio.split(".")[0])
        if familia and familia != dominio:
            observacao = f"possível mesma família de {familia}"

        exemplo = next(
            (
                candidato.get("url")
                for candidato in lista
                if candidato.get("url")
            ),
            "",
        )
        if db.registrar_sugestao_dominio(
            dominio,
            avaliacao["nota"],
            avaliacao["sinais"],
            exemplo,
            contagens,
            observacao,
        ):
            sugestoes.append(
                {
                    "dominio": dominio,
                    "nota": avaliacao["nota"],
                    "sinais": avaliacao["sinais"],
                    **contagens,
                }
            )
    return sugestoes


def registrar_feedback(url: str, status: str) -> bool:
    """Conta confirmados/falsos do domínio quando a revisão decide um candidato.

    `leaked` confirma; `not_me` e `official` contam como falso positivo do
    domínio. O domínio aprovado com mais falsos que confirmados (após o limite
    de revisões) recebe `observacao = "revisar"`; nada é removido.
    """
    from app import db

    confirmado = {"leaked": True, "not_me": False, "official": False}.get(status)
    if confirmado is None:
        return False
    dominio = normalizar_dominio(pontuacao.dominio_de(url))
    if not dominio:
        return False
    return db.registrar_feedback_dominio(
        dominio, confirmado, limite=limite_revisoes()
    )

