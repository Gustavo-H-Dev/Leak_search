"""Hosts aprovados de hospedagem de arquivos e indices de albuns.

A tabela `dominio_suspeito` ainda nao existe, entao a lista vem do arquivo
`app/core/hosts_busca.json` (ou do caminho em `HOSTS_BUSCA_ARQUIVO`). Hosts
desses tipos sao usados nas consultas M2/M3 e na pontuacao como sinal forte
quando o nome-base aparece no titulo.
"""

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

CAMINHO_PADRAO = Path(__file__).with_name("hosts_busca.json")
TIPOS_CONSULTA = ("hospedagem_arquivos", "indice_albuns")
TIPOS_SINAL_FORTE = ("hospedagem_arquivos", "indice_albuns")


def _arquivo() -> Path:
    caminho = os.getenv("HOSTS_BUSCA_ARQUIVO", "").strip()
    return Path(caminho) if caminho else CAMINHO_PADRAO


@lru_cache(maxsize=1)
def _carregar() -> tuple[dict, ...]:
    try:
        dados = json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        logger.warning("Nao foi possivel ler hosts de busca: %s", erro)
        return ()
    entradas = []
    for item in (dados or {}).get("hosts") or ():
        host = (item.get("host") or "").strip().lower().removeprefix("www.")
        if not host:
            continue
        tipo = (item.get("tipo") or "").strip().lower()
        termo = (item.get("nome_consulta") or host.split(".")[0]).strip().lower()
        padroes = []
        for padrao in item.get("padroes_grupo") or ():
            try:
                padroes.append(re.compile(padrao))
            except re.error as erro:
                logger.warning("Padrao de grupo invalido em %s: %s", host, erro)
        entradas.append(
            {"host": host, "tipo": tipo, "termo": termo, "padroes": tuple(padroes)}
        )
    return tuple(entradas)


def hosts(tipos=None) -> list[dict]:
    """Hosts aprovados; opcionalmente filtrados pelos tipos informados."""
    aprovados = _carregar()
    if tipos is None:
        return [dict(item) for item in aprovados]
    tipos = tuple(tipos)
    return [dict(item) for item in aprovados if item["tipo"] in tipos]


def tipo_do_host(host: str) -> str:
    host = (host or "").lower().removeprefix("www.")
    if not host:
        return ""
    for item in _carregar():
        if host == item["host"] or host.endswith("." + item["host"]):
            return item["tipo"]
    return ""
