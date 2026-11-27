"""Fluxo unico de busca: Instagram primeiro, provedor de texto depois."""

import logging

from app import db
from app.core import (
    busca_provedores,
    consultas_vazamento,
    instagram,
    pontuacao,
    serpapi,
    tinyfish,
)

logger = logging.getLogger(__name__)


def _consultas_por_execucao(limite: int | None) -> int:
    if limite:
        return max(1, int(limite))
    try:
        return max(1, int(db.obter_configuracao("busca_maxima_consultas", "12")))
    except (TypeError, ValueError):
        return 12


def _arrobas_da_criadora(criadora: dict) -> list[str]:
    propria = (criadora.get("public_handle") or "").strip().lstrip("@")
    arrobas = [propria] if propria else []
    for item in db.listar_arrobas_descobertos(criadora["id"]):
        arroba = (item.get("arroba") or "").strip().lstrip("@")
        if arroba and arroba.lower() not in {a.lower() for a in arrobas}:
            arrobas.append(arroba)
    return arrobas


def _passo_instagram(criadora: dict) -> tuple[str, list[str]]:
    """Roda o Instagram (respeitando o cache de 24 h) e devolve (erro, arrobas)."""
    handle = (criadora.get("public_handle") or "").strip().lstrip("@")
    erro_instagram = ""
    if handle and db.deve_buscar_instagram(criadora["id"]):
        try:
            perfil, posts = instagram.buscar_postagens(handle)
        except instagram.BuscaInstagramIndisponivel as erro:
            erro_instagram = str(erro)
            logger.warning(
                "Instagram indisponivel para %s: %s", criadora["id"], erro
            )
        except Exception:
            erro_instagram = "Falha inesperada na busca do Instagram."
            logger.exception("Falha inesperada na busca do Instagram")
        else:
            db.salvar_postagens_instagram(criadora["id"], posts)
            db.registrar_busca_instagram(criadora["id"])
            for item in instagram.extrair_arrobas(
                perfil.get("biografia", ""), handle
            ):
                db.salvar_arroba_descoberto(
                    criadora["id"],
                    item.get("plataforma", ""),
                    item["arroba"],
                    item.get("origem", ""),
                )
    return erro_instagram, _arrobas_da_criadora(criadora)


def executar(
    criadora: dict,
    provedor: str,
    *,
    termos,
    sites,
    limite: int | None = None,
    engine: str = "google_images_light",
    parametros: dict | None = None,
    consultas=None,
) -> dict:
    """Executa a busca completa e devolve o resumo final da execucao."""
    criadora_id = criadora["id"]
    total_consultas = _consultas_por_execucao(limite)
    execucao_id = db.criar_busca_execucao(criadora_id, provedor, termos)

    erro_instagram, arrobas = _passo_instagram(criadora)
    if consultas is None:
        consultas = consultas_vazamento.gerar_consultas_busca(
            arrobas, termos, sites, total_consultas
        )
    else:
        consultas = list(consultas)[:total_consultas]

    resumo = {
        "execucao_id": execucao_id,
        "provedor": provedor,
        "arrobas": arrobas,
        "consultas": consultas,
        "total_resultados": 0,
        "unicos": 0,
        "novos": 0,
        "atualizados": 0,
        "descartados": 0,
        "oficiais": 0,
        "duplicados": 0,
        "nota_minima": pontuacao.limiar_revisao(),
        "erros": [],
        "por_consulta": [],
        "erro_instagram": erro_instagram,
    }

    if not consultas:
        mensagem = (
            "Nenhuma consulta pode ser montada: cadastre o @ no perfil de busca."
        )
        db.atualizar_busca_execucao(
            execucao_id,
            status="erro",
            erro_instagram=erro_instagram,
            erro=mensagem,
            resumo=resumo,
        )
        resumo["erro"] = mensagem
        return resumo

    encontrados = []
    falhas = 0
    for consulta in consultas:
        try:
            resultados = busca_provedores.buscar(
                provedor,
                consulta,
                engine=engine,
                parametros=parametros,
            )
        except serpapi.SemResultados:
            resultados = []
        except (serpapi.ErroSerpApi, tinyfish.ErroTinyFish) as erro:
            falhas += 1
            resumo["erros"].append(f"{consulta}: {erro}")
            resultados = []
        resumo["por_consulta"].append(
            {"consulta": consulta, "resultados": len(resultados)}
        )
        resumo["total_resultados"] += len(resultados)
        encontrados.extend(resultados)

    unicos: dict[str, object] = {}
    for candidato in encontrados:
        if candidato.url in unicos:
            resumo["duplicados"] += 1
            continue
        unicos[candidato.url] = candidato
    resumo["unicos"] = len(unicos)

    paginas_oficiais = [
        pagina["url"]
        for pagina in db.listar_paginas_oficiais_com_donos()
        if pagina["user_id"] == criadora_id
    ]

    para_salvar = []
    for candidato in unicos.values():
        avaliacao = pontuacao.avaliar(
            candidato.url,
            candidato.titulo,
            candidato.trecho,
            arrobas=arrobas,
            dominios_vazamento=sites,
            paginas_oficiais=paginas_oficiais,
        )
        if avaliacao["classificacao"] == "oficial":
            resumo["oficiais"] += 1
            continue
        if avaliacao["nota"] < resumo["nota_minima"]:
            resumo["descartados"] += 1
            continue
        para_salvar.append(
            {
                "url_normalizada": candidato.url,
                "dominio": candidato.dominio,
                "titulo": candidato.titulo,
                "trecho": candidato.trecho,
                "miniatura_url": candidato.miniatura_url,
                "provedor": candidato.provedor,
                "origem": candidato.dominio,
                "nota": avaliacao["nota"],
                "motivos": avaliacao["motivos"],
                "classificacao": avaliacao["classificacao"],
                "resposta_bruta": candidato.resposta_bruta,
            }
        )

    novos, atualizados = db.salvar_candidatos_busca(
        criadora_id, execucao_id, para_salvar
    )
    resumo["novos"] = novos
    resumo["atualizados"] = atualizados

    if not encontrados and falhas == len(consultas):
        resumo["erro"] = resumo["erros"][0] if resumo["erros"] else "A busca falhou."
        db.atualizar_busca_execucao(
            execucao_id,
            status="erro",
            erro_instagram=erro_instagram,
            erro=resumo["erro"],
            resumo=resumo,
        )
        return resumo

    db.atualizar_busca_execucao(
        execucao_id,
        status="concluida",
        erro_instagram=erro_instagram,
        resumo=resumo,
    )
    return resumo
