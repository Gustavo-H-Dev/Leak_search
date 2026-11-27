"""Fila de revisao: agrupamento, acoes, desfazer e sugestao de ignorar dominio.

Mapeamento das acoes para os status que ja existiam em `search_candidates`:

- "É meu"  -> `leaked`  (vazado; entra em `leak_reports` e conta como "no ar")
- "Não é"  -> `not_me`  (falso positivo; soma em `n_falsos` do dominio)
- "Não sei" -> `unsure` (novo status; sai da fila de novos sem confirmar nada)

`official` continua valendo para o que ja existia (revisao antiga) e conta como
falso positivo do dominio, mas nao aparece na fila nova.
"""

import json

from app import db
from app.core import dominios_suspeitos, pontuacao

STATUS = {
    "pending": "Novos",
    "leaked": "No ar",
    "not_me": "Não é",
    "unsure": "Não sei",
    "official": "Oficiais",
}
ACOES = (
    ("leaked", "É meu", "primario"),
    ("not_me", "Não é", "secundario"),
    ("unsure", "Não sei", "fantasma"),
)
# (confirmados, falsos) somados no dominio a cada decisao.
FEEDBACK = {"leaked": (1, 0), "not_me": (0, 1), "official": (0, 1)}
LIMITE_IGNORAR = 3
POR_PAGINA = 10
ORDENS = {"nota": "Maior nota", "recentes": "Mais recentes"}
DIAS_FILTRO = ((0, "Qualquer data"), (7, "Últimos 7 dias"), (30, "Últimos 30 dias"), (90, "Últimos 90 dias"))


def decodificar_motivos(valor) -> list[str]:
    if isinstance(valor, list):
        return [str(item) for item in valor]
    try:
        dados = json.loads(valor or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in dados] if isinstance(dados, list) else []


def decodificar_item(linha) -> dict:
    item = dict(linha)
    item["motivos"] = decodificar_motivos(item.get("motivos_json"))
    item["novo"] = bool(item.get("novo_para_criadora"))
    item["engine_nome"] = {
        "serpapi": "SerpApi",
        "tinyfish": "TinyFish",
    }.get(item.get("engine") or "", item.get("engine") or "")
    return item


def agrupar(candidatos) -> list[dict]:
    """Um card por `chave_grupo`; os demais viram "outros" dentro do card."""
    grupos = []
    por_chave: dict[str, dict] = {}
    for linha in candidatos:
        item = decodificar_item(linha)
        chave = item.get("chave_grupo") or ""
        if chave and chave in por_chave:
            por_chave[chave]["outros"].append(item)
            por_chave[chave]["ids"].append(item["id"])
            continue
        grupo = {"principal": item, "outros": [], "ids": [item["id"]]}
        if chave:
            por_chave[chave] = grupo
        grupos.append(grupo)
    return grupos


def _ajustar_feedback(url: str, status: str, sinal: int) -> None:
    if status not in FEEDBACK:
        return
    confirmados, falsos = FEEDBACK[status]
    try:
        dominios_suspeitos.ajustar_feedback(
            url, confirmados * sinal, falsos * sinal
        )
    except Exception:
        pass


def aplicar(criadora_id: int, ids, status: str, autor_id: int) -> dict:
    """Aplica a acao em todos os ids do card e devolve o que o desfazer precisa."""
    ids = [int(item) for item in ids]
    if status not in {"leaked", "not_me", "unsure", "official"}:
        raise ValueError("Ação de revisão inválida.")
    if not ids:
        raise ValueError("Nenhum candidato selecionado.")
    candidatos = []
    for candidato_id in ids:
        linha = db.obter_candidato(candidato_id, criadora_id)
        if linha is None:
            raise LookupError("Candidato não encontrado.")
        candidatos.append(dict(linha))

    anteriores = {}
    leaks_criados = []
    for candidato in candidatos:
        anteriores[str(candidato["id"])] = candidato["status"]
        db.atualizar_status_candidato(candidato["id"], criadora_id, status)
        if status in FEEDBACK:
            try:
                dominios_suspeitos.registrar_feedback(candidato["url"], status)
            except Exception:
                pass
        if status == "leaked":
            if not db.site_vazado_existe(criadora_id, candidato["url"]):
                leaks_criados.append(candidato["url"])
            db.adicionar_site_vazado(
                criadora_id,
                autor_id,
                candidato["url"],
                candidato["title"],
                candidato["source"],
                candidato["snippet"],
                candidato["thumbnail"],
                candidato["engine"],
                {"origem": "revisao", "candidato_id": candidato["id"]},
            )
        elif status == "official":
            url_oficial = pontuacao.normalizar_url(candidato["url"])
            if url_oficial:
                db.adicionar_pagina_oficial(
                    criadora_id,
                    url_oficial,
                    pontuacao.dominio_de(url_oficial),
                    origem="revisao",
                )
            if candidato.get("phash"):
                db.adicionar_hash_oficial(
                    criadora_id, candidato["url"], candidato["phash"]
                )

    sugestao = None
    if status == "not_me" and candidatos:
        dominio = candidatos[0].get("dominio") or ""
        if dominio and not db.dominio_ignorado(dominio):
            total = db.contar_candidatos_dominio_status(
                criadora_id, dominio, "not_me"
            )
            if total >= LIMITE_IGNORAR:
                sugestao = {"dominio": dominio, "total": total}
    return {
        "anteriores": anteriores,
        "leaks_criados": leaks_criados,
        "sugerir_ignorar": sugestao,
    }


def desfazer(criadora_id: int, anteriores: dict, leaks_criados=()) -> int:
    """Volta cada candidato ao status anterior e desfaz os efeitos da acao."""
    revertidos = 0
    for chave, anterior in (anteriores or {}).items():
        try:
            candidato_id = int(chave)
        except (TypeError, ValueError):
            continue
        if anterior not in db.STATUS_CANDIDATO:
            continue
        candidato = db.obter_candidato(candidato_id, criadora_id)
        if candidato is None:
            continue
        atual = candidato["status"]
        if atual == anterior:
            continue
        _ajustar_feedback(candidato["url"], atual, -1)
        _ajustar_feedback(candidato["url"], anterior, 1)
        db.atualizar_status_candidato(candidato_id, criadora_id, anterior)
        revertidos += 1
    for url in leaks_criados or ():
        db.remover_site_vazado(criadora_id, url)
    return revertidos
