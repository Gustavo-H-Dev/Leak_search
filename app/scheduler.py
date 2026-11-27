"""Scheduler: enfileira as buscas vencidas a cada ciclo.

Roda como processo proprio (servico `scheduler` no compose), nunca dentro do
uvicorn: reiniciar o site nao perde o relogio, e reiniciar o scheduler nao
duplica nada porque a reserva do horario acontece no banco antes do enqueue.
"""

import logging
import os
import time

from app import db
from app.core import agendamento, avisos

logger = logging.getLogger(__name__)
INTERVALO = int(os.getenv("AGENDAMENTO_INTERVALO_SEGUNDOS", "30"))
ABANDONADA_MINUTOS = int(os.getenv("EXECUCAO_ABANDONADA_MINUTOS", "90"))


def executar_ciclo() -> list[dict]:
    """Um ciclo completo: fecha execucoes presas e dispara as vencidas."""
    fechadas = db.marcar_execucoes_abandonadas(minutos=ABANDONADA_MINUTOS)
    if fechadas:
        logger.warning("Fechadas %s execucao(oes) interrompida(s)", fechadas)
    resultados = agendamento.processar_vencidos()
    try:
        avisos.processar_resumos()
        avisos.enviar_pendentes()
    except Exception:
        logger.exception("Falha no ciclo de avisos")
    return resultados


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    db.iniciar_banco()
    logger.info("Scheduler de buscas iniciado (intervalo de %ss)", INTERVALO)
    while True:
        try:
            for item in executar_ciclo():
                logger.info(
                    "Criadora %s: execucao %s (%s)",
                    item["criadora_id"],
                    item["execucao_id"],
                    item["mensagem"],
                )
        except Exception:
            logger.exception("Falha no ciclo do scheduler")
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
