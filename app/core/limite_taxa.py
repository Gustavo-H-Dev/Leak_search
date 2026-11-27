"""Limite global de requisicoes ao provedor de texto.

O limite e de 30 buscas por minuto no TinyFish para todas as criadoras juntas.
Com mais de um processo (web, worker, scheduler) um contador em memoria nao
serve, entao a janela deslizante vive no Redis. Sem Redis, cai para uma janela
em memoria: continua correta dentro de um processo so, e o proprio Redis e
necessario para a fila existir.

A janela e deslizante de verdade: 30 requisicoes em qualquer intervalo de 60 s,
nao 30 por minuto de relogio (que deixaria passar 60 numa virada de minuto).
"""

import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

JANELA_SEGUNDOS = 60.0
ESPERA_MAXIMA = 5.0


def limite_por_minuto() -> int:
    try:
        return max(1, int(os.getenv("TINYFISH_REQUISICOES_POR_MINUTO", "30")))
    except ValueError:
        return 30


class StoreMemoria:
    """Janela deslizante em memoria, com relogio injetavel para os testes."""

    def __init__(self):
        self._eventos = defaultdict(deque)
        self._uso = defaultdict(int)
        self._trava = threading.Lock()

    def permitir(self, chave: str, limite: int, agora: float) -> float:
        """0 quando pode seguir; senao, quantos segundos faltam para liberar."""
        with self._trava:
            fila = self._eventos[chave]
            while fila and agora - fila[0] >= JANELA_SEGUNDOS:
                fila.popleft()
            if len(fila) < limite:
                fila.append(agora)
                return 0.0
            return max(0.01, fila[0] + JANELA_SEGUNDOS - agora)

    def registrar_uso(self, chave: str, agora: float) -> None:
        minuto = int(agora // 60)
        with self._trava:
            self._uso[minuto] += 1
            for antigo in [m for m in self._uso if m < minuto - 120]:
                self._uso.pop(antigo, None)

    def estatisticas(self, chave: str, agora: float) -> dict:
        minuto = int(agora // 60)
        with self._trava:
            valores = [self._uso.get(minuto - indice, 0) for indice in range(59, -1, -1)]
        return {"minutos": valores}


_SCRIPT = """
local chave = KEYS[1]
local agora = tonumber(ARGV[1])
local janela = tonumber(ARGV[2])
local limite = tonumber(ARGV[3])
local marca = ARGV[4]
redis.call('ZREMRANGEBYSCORE', chave, 0, agora - janela)
local total = redis.call('ZCARD', chave)
if total < limite then
  redis.call('ZADD', chave, agora, marca)
  redis.call('PEXPIRE', chave, math.floor(janela * 1000))
  return 0
end
local mais_antigo = redis.call('ZRANGE', chave, 0, 0, 'WITHSCORES')
if mais_antigo[2] then
  return math.max(0.01, tonumber(mais_antigo[2]) + janela - agora)
end
return janela
"""


class StoreRedis:
    """Mesma janela, compartilhada por todos os processos via Redis."""

    def __init__(self, cliente):
        self._cliente = cliente

    def permitir(self, chave: str, limite: int, agora: float) -> float:
        marca = f"{agora:.6f}-{uuid.uuid4().hex[:8]}"
        espera = self._cliente.eval(
            _SCRIPT,
            1,
            chave,
            agora,
            JANELA_SEGUNDOS,
            limite,
            marca,
        )
        return float(espera or 0.0)

    def registrar_uso(self, chave: str, agora: float) -> None:
        minuto = int(agora // 60)
        try:
            pipe = self._cliente.pipeline()
            pipe.incr(f"{chave}:uso:{minuto}")
            pipe.expire(f"{chave}:uso:{minuto}", 7200)
            pipe.execute()
        except Exception:
            logger.warning("Nao foi possivel registrar o uso do limite no Redis")

    def estatisticas(self, chave: str, agora: float) -> dict:
        minuto = int(agora // 60)
        chaves = [f"{chave}:uso:{minuto - indice}" for indice in range(59, -1, -1)]
        try:
            valores = self._cliente.mget(chaves)
        except Exception:
            valores = [None] * len(chaves)
        return {"minutos": [int(valor or 0) for valor in valores]}


class Limitador:
    def __init__(self, store, *, chave="tinyfish", dormir=None, relogio=None):
        self._store = store
        self._chave = chave
        self._dormir = dormir or time.sleep
        self._relogio = relogio or time.time

    def aguardar(self) -> None:
        """Bloqueia ate haver vaga na janela; nunca dorme mais de 5 s de uma vez."""
        while True:
            agora = self._relogio()
            espera = self._store.permitir(
                self._chave, limite_por_minuto(), agora
            )
            if espera <= 0:
                try:
                    self._store.registrar_uso(self._chave, agora)
                except Exception:
                    logger.warning("Nao foi possivel registrar o uso do limitador")
                return
            self._dormir(min(espera, ESPERA_MAXIMA))


_limitador: Limitador | None = None


def configurar(limitador: Limitador | None) -> None:
    """Troca o limitador em uso (testes e reinicializacao do processo)."""
    global _limitador
    _limitador = limitador


def _construir() -> Limitador:
    url = os.getenv("REDIS_URL", "").strip()
    if url:
        try:
            import redis

            cliente = redis.Redis.from_url(
                url, socket_timeout=2, socket_connect_timeout=2
            )
            cliente.ping()
            return Limitador(StoreRedis(cliente))
        except Exception:
            logger.warning(
                "Redis indisponivel para o limite global; usando janela local"
            )
    return Limitador(StoreMemoria())


def obter() -> Limitador:
    global _limitador
    if _limitador is None:
        _limitador = _construir()
    return _limitador


def aguardar_tinyfish() -> None:
    obter().aguardar()


def estatisticas() -> dict:
    """Uso do limite de 30/min: minuto atual, pico e total da ultima hora."""
    limitador = obter()
    try:
        dados = limitador._store.estatisticas(limitador._chave, time.time())
        minutos = list(dados.get("minutos") or [])
    except Exception:
        logger.warning("Nao foi possivel ler as estatisticas do limitador")
        minutos = []
    agora = time.time()
    minuto_atual = int(agora // 60)
    rotulos = [
        time.strftime("%H:%M", time.localtime((minuto_atual - indice) * 60))
        for indice in range(len(minutos) - 1, -1, -1)
    ]
    return {
        "limite": limite_por_minuto(),
        "minutos": list(zip(rotulos, minutos)),
        "ultimo_minuto": minutos[-1] if minutos else 0,
        "pico": max(minutos) if minutos else 0,
        "total_60min": sum(minutos),
    }
