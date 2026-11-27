"""Worker RQ: executa as buscas enfileiradas, uma criadora por job.

O job e idempotente. Antes de buscar, ele tenta `reservar_execucao`, que troca
'na_fila' por 'rodando' de forma atomica. Se outro worker (ou uma reentrega do
RQ depois de um reinicio) chegar com a mesma execucao, a reserva falha e o job
nao roda duas vezes.
"""

import logging
import os
import time

from app import db
from app.core import agendamento, consultas_vazamento, fluxo_busca

logger = logging.getLogger(__name__)


def _termos_e_sites() -> tuple[list[str], list[str]]:
    termos = db.obter_configuracao(
        "vazamento_termos", "\n".join(consultas_vazamento.TERMOS_PADRAO)
    )
    sites = db.obter_configuracao(
        "vazamento_sites", "\n".join(consultas_vazamento.SITES_PADRAO)
    )
    return (
        consultas_vazamento.limpar_termos(termos),
        consultas_vazamento.limpar_sites(sites),
    )


def _limite_consultas() -> int:
    try:
        return max(1, int(db.obter_configuracao("busca_maxima_consultas", "18")))
    except (TypeError, ValueError):
        return 18


def classificar_falha(resumo: dict, erro: str = "") -> str:
    """Motivo curto da falha: 402, 429, Instagram bloqueado ou outro."""
    partes = [erro or "", (resumo or {}).get("erro", "")]
    partes.extend((resumo or {}).get("erros", []) or [])
    partes.append((resumo or {}).get("erro_instagram", "") or "")
    texto = " ".join(str(parte) for parte in partes).lower()
    if "402" in texto or "sem saldo" in texto or "sem crédito" in texto:
        return "sem_creditos"
    if "429" in texto or "limite de requisi" in texto:
        return "limite_provedor"
    if "instagram" in texto or "bloque" in texto:
        return "instagram_bloqueado"
    if "configur" in texto:
        return "configuracao"
    return "erro"


def classificar_status(resumo: dict) -> str:
    if (resumo or {}).get("erro"):
        return "erro"
    if (resumo or {}).get("erros") or (resumo or {}).get("erro_instagram"):
        return "avisos"
    return "concluida"


def executar_busca_agendada(criadora_id: int, execucao_id: int, provedor: str = ""):
    inicio = time.monotonic()
    if not db.reservar_execucao(execucao_id):
        logger.info("Execucao %s ja foi pega por outro worker", execucao_id)
        return {"execucao_id": execucao_id, "ignorada": True}

    alvo = db.obter_usuario_por_id(criadora_id)
    if not alvo:
        db.finalizar_busca_execucao(
            execucao_id,
            status="erro",
            motivo_falha="configuracao",
            erro="Conta da criadora não existe mais.",
        )
        return {"execucao_id": execucao_id, "erro": "conta ausente"}

    provedor = provedor or agendamento.provedor_padrao()
    termos, sites = _termos_e_sites()
    try:
        resumo = fluxo_busca.executar(
            alvo,
            provedor,
            termos=termos,
            sites=sites,
            limite=_limite_consultas(),
            execucao_id=execucao_id,
        )
    except Exception as erro:
        logger.exception("Falha na busca agendada da criadora %s", criadora_id)
        db.finalizar_busca_execucao(
            execucao_id,
            status="erro",
            motivo_falha=classificar_falha({}, str(erro)),
            erro=str(erro)[:2000],
        )
        db.registrar_duracao_execucao(execucao_id, (time.monotonic() - inicio) * 1000)
        raise

    status = classificar_status(resumo)
    motivo = ""
    erro = resumo.get("erro", "")
    if status == "erro":
        motivo = classificar_falha(resumo, erro)
    elif status == "avisos":
        motivo = classificar_falha(resumo, erro)
    db.finalizar_busca_execucao(
        execucao_id,
        status=status,
        resumo=resumo,
        erro=erro or "; ".join(resumo.get("erros", []))[:2000],
        motivo_falha=motivo,
        erro_instagram=resumo.get("erro_instagram", ""),
    )
    db.registrar_duracao_execucao(execucao_id, (time.monotonic() - inicio) * 1000)
    return {
        "execucao_id": execucao_id,
        "status": status,
        "novos": resumo.get("novos", 0),
        "motivo": motivo,
    }


def main() -> None:
    from rq import Worker

    from app.core import fila_buscas

    logging.basicConfig(level=logging.INFO)
    db.iniciar_banco()
    fila = fila_buscas.obter_fila()
    logger.info("Worker de buscas ouvindo a fila %s", fila_buscas.NOME_FILA)
    Worker([fila], connection=fila.connection).work()


if __name__ == "__main__":
    main()
