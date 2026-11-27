"""Painel e fila de revisão (fase 4).

Cobre o aceite: nada de outra criadora aparece (nem por GET, nem por POST), 20
itens cabem no celular sem rolagem lateral, e cada ação atualiza os contadores
de domínio, com desfazer.
"""

import json
import os
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import autenticacao
from app.main import app

SENHA = "senha-teste-revisao"
LIMIAR = 30


def _csrf(html: str) -> str:
    achado = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert achado, "token CSRF nao encontrado na pagina"
    return achado.group(1)


def _entrar(cliente: TestClient, usuario: str) -> None:
    token = _csrf(cliente.get("/login").text)
    resposta = cliente.post(
        "/login",
        data={"csrf_token": token, "username": usuario, "senha": SENHA, "next": "/"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303, resposta.text


def _semear(criadora_id, itens):
    execucao_id = db.criar_busca_execucao(criadora_id, "tinyfish", [])
    db.salvar_candidatos_busca(
        criadora_id,
        execucao_id,
        [
            {
                "url_normalizada": item["url"],
                "dominio": item.get("dominio") or "site-vazado.com",
                "titulo": item.get("titulo") or item["url"],
                "trecho": item.get("trecho") or "trecho do resultado",
                "provedor": item.get("engine") or "tinyfish",
                "origem": item.get("engine") or "tinyfish",
                "nota": item.get("nota", 80),
                "motivos": item.get("motivos") or ["nome aparece no título"],
                "classificacao": "provavel_vazamento",
                "chave_grupo": item.get("chave_grupo") or "",
                "resposta_bruta": {},
            }
            for item in itens
        ],
    )


@pytest.fixture()
def ambiente(tmp_path):
    with (
        patch.object(db, "CAMINHO_DB", tmp_path / "revisao.db"),
        patch.dict(
            os.environ,
            {
                "SERPAPI_API_KEY": "",
                "TINYFISH_API_KEY": "",
                "REDIS_URL": "",
                "COMPARACAO_IMAGEM_ATIVA": "false",
            },
        ),
    ):
        db.iniciar_banco()
        ana = db.criar_usuario(
            "ana_revisao",
            "ana_revisao@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Ana",
            public_handle="anaanax",
        )
        bia = db.criar_usuario(
            "bia_revisao",
            "bia_revisao@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Bia",
            public_handle="biaanax",
        )
        db.confirmar_email(ana)
        db.confirmar_email(bia)
        db.semear_dominios(
            [
                {
                    "dominio": "site-vazado.com",
                    "tipo": "hospedagem_arquivos",
                    "status": "aprovado",
                }
            ]
        )
        with TestClient(app) as cliente:
            _entrar(cliente, "ana_revisao")
            yield SimpleNamespace(cliente=cliente, ana=ana, bia=bia)


def _ids(criadora_id):
    return {
        linha["url"]: linha["id"] for linha in db.listar_candidatos(criadora_id)
    }


def _acao(cliente, ids, status):
    token = _csrf(cliente.get("/revisao").text)
    return cliente.post(
        "/revisao/acao",
        data={"csrf_token": token, "ids": ids, "status": status, "target_user_id": 0},
        headers={"Accept": "application/json"},
    )


# --------------------------------------------------------------- isolamento


def test_criadora_nao_ve_candidatos_de_outra(ambiente):
    _semear(ambiente.ana, [{"url": "https://site-vazado.com/ana", "nota": 90}])
    _semear(ambiente.bia, [{"url": "https://site-vazado.com/bia", "nota": 95}])

    pagina = ambiente.cliente.get("/revisao")
    assert pagina.status_code == 200
    assert "https://site-vazado.com/ana" in pagina.text
    assert "https://site-vazado.com/bia" not in pagina.text


def test_acao_em_candidato_alheio_da_404(ambiente):
    _semear(ambiente.bia, [{"url": "https://site-vazado.com/bia", "nota": 95}])
    alheio = _ids(ambiente.bia)["https://site-vazado.com/bia"]

    resposta = _acao(ambiente.cliente, [alheio], "leaked")
    assert resposta.status_code == 404
    assert db.obter_candidato(alheio, ambiente.bia)["status"] == "pending"


# ------------------------------------------------------------- ações e mapa


def test_acoes_mapeiam_status_e_contadores_do_dominio(ambiente):
    _semear(
        ambiente.ana,
        [
            {"url": "https://site-vazado.com/um", "nota": 90},
            {"url": "https://site-vazado.com/dois", "nota": 80},
            {"url": "https://site-vazado.com/tres", "nota": 70},
        ],
    )
    ids = _ids(ambiente.ana)

    assert _acao(ambiente.cliente, [ids["https://site-vazado.com/um"]], "leaked").json()["ok"]
    assert _acao(ambiente.cliente, [ids["https://site-vazado.com/dois"]], "not_me").json()["ok"]
    assert _acao(ambiente.cliente, [ids["https://site-vazado.com/tres"]], "unsure").json()["ok"]

    assert db.obter_candidato(ids["https://site-vazado.com/um"], ambiente.ana)["status"] == "leaked"
    assert db.obter_candidato(ids["https://site-vazado.com/dois"], ambiente.ana)["status"] == "not_me"
    assert db.obter_candidato(ids["https://site-vazado.com/tres"], ambiente.ana)["status"] == "unsure"
    dominio = db.obter_dominio_suspeito("site-vazado.com")
    assert dominio["n_confirmados"] == 1
    assert dominio["n_falsos"] == 1
    assert db.site_vazado_existe(ambiente.ana, "https://site-vazado.com/um")


def test_desfazer_volta_status_contadores_e_relato(ambiente):
    _semear(ambiente.ana, [{"url": "https://site-vazado.com/um", "nota": 90}])
    candidato_id = _ids(ambiente.ana)["https://site-vazado.com/um"]

    salvo = _acao(ambiente.cliente, [candidato_id], "leaked").json()
    assert db.obter_candidato(candidato_id, ambiente.ana)["status"] == "leaked"

    token = _csrf(ambiente.cliente.get("/revisao?status=leaked").text)
    resposta = ambiente.cliente.post(
        "/revisao/desfazer",
        data={
            "csrf_token": token,
            "anteriores": json.dumps(salvo["anteriores"]),
            "leaks_criados": json.dumps(salvo["leaks_criados"]),
            "target_user_id": 0,
        },
        headers={"Accept": "application/json"},
    )
    assert resposta.status_code == 200
    assert resposta.json()["revertidos"] == 1
    assert db.obter_candidato(candidato_id, ambiente.ana)["status"] == "pending"
    assert not db.site_vazado_existe(ambiente.ana, "https://site-vazado.com/um")
    assert db.obter_dominio_suspeito("site-vazado.com")["n_confirmados"] == 0


def test_sugere_ignorar_dominio_apos_tres_nao_e(ambiente):
    _semear(
        ambiente.ana,
        [
            {"url": "https://site-vazado.com/1", "nota": 90},
            {"url": "https://site-vazado.com/2", "nota": 80},
            {"url": "https://site-vazado.com/3", "nota": 70},
        ],
    )
    ids = _ids(ambiente.ana)
    for numero in (1, 2):
        assert _acao(ambiente.cliente, [ids[f"https://site-vazado.com/{numero}"]], "not_me").json()["ok"]
    terceira = _acao(ambiente.cliente, [ids["https://site-vazado.com/3"]], "not_me").json()
    assert terceira["sugerir_ignorar"] == {"dominio": "site-vazado.com", "total": 3}

    token = _csrf(ambiente.cliente.get("/revisao").text)
    ambiente.cliente.post(
        "/revisao/ignorar-dominio",
        data={"csrf_token": token, "dominio": "site-vazado.com", "target_user_id": 0},
        follow_redirects=False,
    )
    assert db.dominio_ignorado("site-vazado.com")


# --------------------------------------------------------- agrupar e filtrar


def test_mesma_chave_grupo_vira_um_card(ambiente):
    _semear(
        ambiente.ana,
        [
            {
                "url": "https://site-vazado.com/album/a1",
                "dominio": "site-vazado.com",
                "chave_grupo": "album-1",
                "nota": 90,
            },
            {
                "url": "https://outro-site.com/albums/a1",
                "dominio": "outro-site.com",
                "chave_grupo": "album-1",
                "nota": 85,
            },
        ],
    )

    pagina = ambiente.cliente.get("/revisao").text
    assert pagina.count("data-card-revisao") == 1
    assert "Mesmo álbum em outros 1 site(s)" in pagina


def test_filtros_por_provedor_e_nota(ambiente):
    _semear(
        ambiente.ana,
        [
            {"url": "https://site-vazado.com/tiny", "nota": 90, "engine": "tinyfish"},
            {"url": "https://site-vazado.com/serp", "nota": 90, "engine": "serpapi"},
            {"url": "https://site-vazado.com/baixo", "nota": 10, "engine": "tinyfish"},
        ],
    )

    sem_filtro = ambiente.cliente.get("/revisao").text
    assert "site-vazado.com/tiny" in sem_filtro
    assert "site-vazado.com/serp" in sem_filtro
    assert "site-vazado.com/baixo" not in sem_filtro

    so_serpapi = ambiente.cliente.get("/revisao?engine=serpapi").text
    assert "site-vazado.com/serp" in so_serpapi
    assert "site-vazado.com/tiny" not in so_serpapi

    inclui_baixo = ambiente.cliente.get("/revisao?nota=10").text
    assert "site-vazado.com/baixo" in inclui_baixo


def test_paginacao_de_dez_cards(ambiente):
    _semear(
        ambiente.ana,
        [
            {"url": f"https://site-vazado.com/p{item}", "nota": 90 - item}
            for item in range(12)
        ],
    )

    primeira = ambiente.cliente.get("/revisao").text
    assert primeira.count("data-card-revisao") == 10
    assert "Página 1 de 2" in primeira

    segunda = ambiente.cliente.get("/revisao?pagina=2").text
    assert segunda.count("data-card-revisao") == 2


def test_vinte_itens_sem_rolagem_lateral(ambiente):
    _semear(
        ambiente.ana,
        [
            {
                "url": f"https://site-vazado.com/longo/{item}",
                "titulo": "Título muito comprido " * 6,
                "nota": 90 - item,
            }
            for item in range(20)
        ],
    )
    fila = ambiente.cliente.get("/revisao")
    assert fila.status_code == 200
    # Nenhuma tabela ou largura fixa que force rolagem no celular.
    assert "<table" not in fila.text
    assert "min-w-0" in fila.text
    assert "break-words" in fila.text


# --------------------------------------------------------------- painel/etc


def test_painel_conta_fila_e_varreduras(ambiente):
    _semear(
        ambiente.ana,
        [
            {"url": "https://site-vazado.com/1", "nota": 90},
            {"url": "https://site-vazado.com/2", "nota": 80},
        ],
    )
    candidato_id = _ids(ambiente.ana)["https://site-vazado.com/1"]
    _acao(ambiente.cliente, [candidato_id], "leaked")

    painel = ambiente.cliente.get("/")
    assert painel.status_code == 200
    assert "Novos para revisar" in painel.text
    assert "No ar" in painel.text
    assert "Revisar 1 novo(s)" in painel.text
    assert "Última varredura" in painel.text


def test_situacao_da_remocao_muda_no_painel(ambiente):
    _semear(ambiente.ana, [{"url": "https://site-vazado.com/1", "nota": 90}])
    candidato_id = _ids(ambiente.ana)["https://site-vazado.com/1"]
    _acao(ambiente.cliente, [candidato_id], "leaked")

    token = _csrf(ambiente.cliente.get("/revisao?status=leaked").text)
    ambiente.cliente.post(
        f"/revisao/{candidato_id}/remocao",
        data={
            "csrf_token": token,
            "situacao": "em_remocao",
            "status": "leaked",
            "target_user_id": 0,
        },
        follow_redirects=False,
    )
    assert db.situacoes_leaks(ambiente.ana)["https://site-vazado.com/1"] == "em_remocao"
    assert db.contar_painel_revisao(ambiente.ana, LIMIAR)["em_remocao"] == 1


def test_link_externo_so_aparece_com_aviso(ambiente):
    _semear(ambiente.ana, [{"url": "https://site-vazado.com/1", "nota": 90}])
    pagina = ambiente.cliente.get("/revisao").text
    assert "Abrir site (pode conter conteúdo adulto)" in pagina
    assert 'rel="noopener noreferrer nofollow"' in pagina
    assert pagina.index("pode conter conteúdo adulto") < pagina.index(
        'href="https://site-vazado.com/1"'
    )
