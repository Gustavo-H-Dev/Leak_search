"""Fila RQ das buscas. Sem REDIS_URL nao ha fila: o app avisa em vez de fingir."""

import logging
import os

logger = logging.getLogger(__name__)

NOME_FILA = "buscas"
TEMPO_MAXIMO_JOB = 3600


def redis_url() -> str:
    return os.getenv("REDIS_URL", "").strip()


def configurada() -> bool:
    return bool(redis_url())


def obter_fila():
    """Queue do RQ ligada ao Redis configurado (import tardio de proposito)."""
    from redis import Redis
    from rq import Queue

    url = redis_url()
    if not url:
        raise RuntimeError("REDIS_URL nao configurada")
    conexao = Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
    return Queue(NOME_FILA, connection=conexao, default_timeout=TEMPO_MAXIMO_JOB)


def enfileirar(criadora_id: int, execucao_id: int, provedor: str) -> dict:
    if not configurada():
        return {
            "ok": False,
            "mensagem": (
                "A fila não está configurada: defina REDIS_URL no servidor "
                "para rodar buscas em segundo plano."
            ),
        }
    try:
        obter_fila().enqueue(
            "app.worker.executar_busca_agendada",
            criadora_id,
            execucao_id,
            provedor,
            job_timeout=TEMPO_MAXIMO_JOB,
            failure_ttl=7 * 24 * 3600,
        )
    except Exception:
        logger.exception("Falha ao enfileirar a busca da criadora %s", criadora_id)
        return {
            "ok": False,
            "mensagem": (
                "Não foi possível falar com a fila (Redis). "
                "A busca não foi iniciada; tente de novo em instantes."
            ),
        }
    return {"ok": True, "mensagem": "Busca na fila."}
