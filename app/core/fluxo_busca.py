"""Fluxo unico de busca: Instagram primeiro, provedor de texto depois."""

import logging
from datetime import datetime, timezone

from app import db
from app.core import (
    busca_provedores,
    consultas_vazamento,
    instagram,
    pontuacao,
    serpapi,
    tinyfish,
    variantes_nome,
)

logger = logging.getLogger(__name__)
CHAVE_PERFIL_INSTAGRAM = "perfil_instagram"


def _consultas_por_execucao(limite: int | None) -> int:
    if limite:
        return max(1, int(limite))
    try:
        return max(1, int(db.obter_configuracao("busca_maxima_consultas", "18")))
    except (TypeError, ValueError):
        return 18


def registrar_perfil_instagram(criadora_id: int, handle: str, perfil: dict) -> list[str]:
    """Guarda bio, arrobas descobertos e links do perfil para a classificacao.

    Os links citados na bio (site proprio, linktree, loja) viram paginas
    oficiais automaticas, sem a criadora precisar cadastrar nada.
    """
    bio = (perfil or {}).get("biografia", "") or ""
    for item in instagram.extrair_arrobas(bio, handle):
        db.salvar_arroba_descoberto(
            criadora_id,
            item.get("plataforma", ""),
            item["arroba"],
            item.get("origem", ""),
        )
    links = instagram.extrair_links(bio)
    db.salvar_snapshot_busca(
        criadora_id,
        CHAVE_PERFIL_INSTAGRAM,
        {
            "handle": handle,
            "nome": (perfil or {}).get("nome", ""),
            "bio": bio[:4000],
            "links": links,
            "atualizado_em": datetime.now(timezone.utc).isoformat(),
        },
    )
    return links


def links_oficiais(criadora_id: int) -> list[str]:
    snapshot = db.obter_snapshot_busca(criadora_id, CHAVE_PERFIL_INSTAGRAM)
    if not snapshot:
        return []
    dados = snapshot.get("dados") or {}
    return [str(link) for link in dados.get("links") or [] if link]


def _arrobas_da_criadora(criadora: dict) -> list[str]:
    propria = (criadora.get("public_handle") or "").strip().lstrip("@")
    arrobas = [propria] if propria else []
    for item in db.listar_arrobas_descobertos(criadora["id"]):
        arroba = (item.get("arroba") or "").strip().lstrip("@")
        if arroba and arroba.lower() not in {a.lower() for a in arrobas}:
            arrobas.append(arroba)
    return arrobas


def _passo_instagram(criadora: dict) -> tuple[str, list[str], bool]:
    """Roda o Instagram (respeitando o cache de 24 h).

    Devolve (erro, arrobas, arroba_invalida). `arroba_invalida` interrompe a
    busca: com o @ errado as consultas nao encontrariam nada util.
    """
    handle = (criadora.get("public_handle") or "").strip().lstrip("@")
    erro_instagram = ""
    arroba_invalida = False
    if handle and db.deve_buscar_instagram(criadora["id"]):
        try:
            dados = instagram.buscar_perfil(handle)
        except instagram.PerfilInstagramNaoEncontrado as erro:
            erro_instagram = str(erro)
            arroba_invalida = True
            logger.warning("Perfil do Instagram nao existe: %s", erro)
        except instagram.BuscaInstagramIndisponivel as erro:
            erro_instagram = str(erro)
            logger.warning(
                "Instagram indisponivel para %s: %s", criadora["id"], erro
            )
        except Exception:
            erro_instagram = "Falha inesperada na busca do Instagram."
            logger.exception("Falha inesperada na busca do Instagram")
        else:
            perfil = dados["perfil"]
            registrar_perfil_instagram(criadora["id"], handle, perfil)
            try:
                posts = instagram.montar_posts(handle, dados["posts"])
            except instagram.BuscaInstagramIndisponivel as erro:
                erro_instagram = str(erro)
                logger.warning("Instagram sem postagens utilizaveis: %s", erro)
            else:
                db.salvar_postagens_instagram(criadora["id"], posts)
                db.registrar_busca_instagram(criadora["id"])
    return erro_instagram, _arrobas_da_criadora(criadora), arroba_invalida


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
    # SQLite devolve sqlite3.Row (sem .get); o fluxo usa dicionario.
    criadora = dict(criadora)
    criadora_id = criadora["id"]
    total_consultas = _consultas_por_execucao(limite)
    execucao_id = db.criar_busca_execucao(criadora_id, provedor, termos)

    nome = (criadora.get("public_name") or "").strip()
    erro_instagram, arrobas, arroba_invalida = _passo_instagram(criadora)
    variantes = variantes_nome.variantes_dos_arrobas(arrobas)
    nome_base = variantes_nome.nome_base(arrobas)
    if consultas is None:
        planos = consultas_vazamento.gerar_consultas_priorizadas(
            arrobas, termos, sites, total_consultas, nome=nome
        )
    else:
        planos = [
            {"modelo": "manual", "consulta": consulta, "parametros": {}}
            for consulta in list(consultas)[:total_consultas]
        ]
    consultas = [plano["consulta"] for plano in planos]

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
        "variantes": variantes,
        "nome_base": nome_base,
        "grupos": 0,
    }

    if arroba_invalida:
        mensagem = erro_instagram or (
            "O Instagram nao encontrou o @ cadastrado. Corrija o perfil de busca."
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
    origem_consulta: dict[str, tuple[int | None, int]] = {}
    for plano in planos:
        consulta = plano["consulta"]
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
        try:
            log_id = db.registrar_consulta_log(
                execucao_id,
                plano["modelo"],
                consulta,
                plano.get("parametros"),
                [
                    {"url": candidato.url, "posicao": posicao}
                    for posicao, candidato in enumerate(resultados, start=1)
                ],
                resultados[0].resposta_bruta if resultados else {},
            )
        except Exception:
            logger.exception("Falha ao registrar a consulta no log")
            log_id = None
        for posicao, candidato in enumerate(resultados, start=1):
            origem_consulta.setdefault(candidato.url, (log_id, posicao))
        resumo["por_consulta"].append(
            {
                "consulta": consulta,
                "modelo": plano["modelo"],
                "resultados": len(resultados),
            }
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
    # Links do perfil/bio do Instagram entram como paginas oficiais automaticas.
    paginas_oficiais.extend(links_oficiais(criadora_id))

    para_salvar = []
    chaves_vistas: set[str] = set()
    for candidato in unicos.values():
        avaliacao = pontuacao.avaliar(
            candidato.url,
            candidato.titulo,
            candidato.trecho,
            arrobas=arrobas,
            dominios_vazamento=sites,
            paginas_oficiais=paginas_oficiais,
            nome=nome,
            variantes=variantes,
            nome_base=nome_base,
        )
        if avaliacao["classificacao"] == "oficial":
            resumo["oficiais"] += 1
            continue
        if avaliacao["nota"] < resumo["nota_minima"]:
            resumo["descartados"] += 1
            continue
        log_id, posicao = origem_consulta.get(candidato.url, (None, 0))
        chave = pontuacao.chave_grupo(candidato.url)
        if chave:
            chaves_vistas.add(chave)
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
                "chave_grupo": chave,
                "consulta_log_id": log_id,
                "posicao_busca": posicao,
            }
        )
    resumo["grupos"] = len(chaves_vistas)

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
