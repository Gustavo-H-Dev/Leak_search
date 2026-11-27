"""Onboarding de 4 passos e perfis monitorados (fase 1).

Percorre o caminho HTTP real: a criadora salva perfis, variações e páginas,
retoma pelo `?passo=` e dispara a primeira busca.
"""

import os
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import autenticacao, fluxo_busca, perfis_monitorados
from app.main import app

SENHA = "senha-teste-onboarding"


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


@pytest.fixture()
def criadora(tmp_path):
    with (
        patch.object(db, "CAMINHO_DB", tmp_path / "onboarding.db"),
        patch.dict(
            os.environ,
            {
                "SERPAPI_API_KEY": "",
                "TINYFISH_API_KEY": "",
                "PERFIS_MONITORADOS_LIMITE": "2",
            },
        ),
    ):
        db.iniciar_banco()
        user_id = db.criar_usuario(
            "criadora_nova",
            "criadora_nova@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Mari",
            public_handle="marianax",
        )
        db.confirmar_email(user_id)
        with TestClient(app) as cliente:
            _entrar(cliente, "criadora_nova")
            yield SimpleNamespace(cliente=cliente, id=user_id)


def test_onboarding_abre_no_passo_1_com_quatro_etapas(criadora):
    pagina = criadora.cliente.get("/onboarding")
    assert pagina.status_code == 200
    assert "Passo 1 de 4" in pagina.text
    assert "aria-current=\"step\"" in pagina.text
    for titulo in ("Perfis", "Variações", "Páginas oficiais", "Resumo"):
        assert titulo in pagina.text


def test_passo_1_salva_perfis_e_respeita_o_limite(criadora):
    token = _csrf(criadora.cliente.get("/onboarding").text)
    resposta = criadora.cliente.post(
        "/onboarding/perfis",
        data={
            "csrf_token": token,
            "plataforma": ["instagram", "tiktok"],
            "arroba": ["@Marianax", "marianax"],
        },
        follow_redirects=True,
    )
    assert resposta.status_code == 200
    assert "2 perfil(is) salvo(s)." in resposta.text

    perfis = db.listar_perfis_monitorados(criadora.id)
    assert {(p["plataforma"], p["arroba"]) for p in perfis} == {
        ("instagram", "marianax"),
        ("tiktok", "marianax"),
    }
    assert all(p["status"] == "ativo" for p in perfis)

    excedente = criadora.cliente.post(
        "/onboarding/perfis",
        data={
            "csrf_token": token,
            "plataforma": ["youtube"],
            "arroba": ["marianax"],
        },
        follow_redirects=True,
    )
    assert "limite de 2 perfis monitorados" in excedente.text
    assert len(db.listar_perfis_monitorados(criadora.id)) == 2


def test_passo_2_guarda_variacoes_e_desliga_as_nao_marcadas(criadora):
    token = _csrf(criadora.cliente.get("/onboarding").text)
    criadora.cliente.post(
        "/onboarding/perfis",
        data={
            "csrf_token": token,
            "plataforma": ["instagram"],
            "arroba": ["marianax"],
        },
    )
    criadora.cliente.post(
        "/onboarding/variantes",
        data={
            "csrf_token": token,
            "nova_variante": ["mariana", "marianax"],
            "texto_novo": "mari",
        },
        follow_redirects=True,
    )
    variantes = {v["texto"]: v for v in db.listar_variantes_nome(criadora.id)}
    assert set(variantes) == {"mariana", "marianax", "mari"}
    assert all(int(v["ativo"]) for v in variantes.values())

    mariana = variantes["mariana"]["id"]
    criadora.cliente.post(
        "/onboarding/variantes",
        data={"csrf_token": token, "variante_ativa": [mariana]},
        follow_redirects=True,
    )
    ligadas = [v["texto"] for v in db.listar_variantes_nome(criadora.id) if v["ativo"]]
    assert ligadas == ["mariana"]

    pagina = criadora.cliente.get("/onboarding?passo=2").text
    assert "1 ligadas" in pagina


def test_passo_3_normaliza_a_pagina_e_pode_ser_pulado(criadora):
    token = _csrf(criadora.cliente.get("/onboarding").text)
    resposta = criadora.cliente.post(
        "/onboarding/oficiais",
        data={
            "csrf_token": token,
            "url": "https://www.Instagram.com/Marianax/?hl=pt",
        },
        follow_redirects=True,
    )
    assert "Página oficial adicionada." in resposta.text
    paginas = db.listar_paginas_oficiais(criadora.id)
    assert len(paginas) == 1
    assert paginas[0]["dominio"] == "instagram.com"
    assert paginas[0]["url"] == "https://instagram.com/Marianax"
    assert paginas[0]["origem"] == "manual"

    criadora.cliente.post(
        "/onboarding/oficiais/pular",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert perfis_monitorados.estado(criadora.id)["pulou_oficiais"] is True


def test_dados_da_busca_usam_so_variantes_ativas_e_ignoram_oficiais(criadora):
    perfis_monitorados.salvar_perfil(criadora.id, "instagram", "marianax")
    perfis_monitorados.salvar_variantes(
        criadora.id, ["mariana", "marianax"], origem="manual"
    )
    variantes = {v["texto"]: v["id"] for v in db.listar_variantes_nome(criadora.id)}
    db.atualizar_variante_nome(variantes["marianax"], criadora.id, False)
    perfis_monitorados.salvar_pagina(criadora.id, "https://marianax.com/links")

    arrobas, textos, paginas = fluxo_busca.dados_do_perfil(
        dict(db.obter_usuario_por_id(criadora.id))
    )
    assert arrobas == ["marianax"]
    assert textos == ["mariana"]
    assert paginas == ["https://marianax.com/links"]


def test_configuracoes_perfis_mostra_e_remove(criadora):
    perfis_monitorados.salvar_perfil(criadora.id, "instagram", "marianax")
    pagina = criadora.cliente.get("/configuracoes/perfis")
    assert pagina.status_code == 200
    assert "@marianax" in pagina.text

    perfil = db.listar_perfis_monitorados(criadora.id)[0]
    token = _csrf(pagina.text)
    resposta = criadora.cliente.post(
        f"/onboarding/perfis/{perfil['id']}/excluir",
        data={"csrf_token": token, "voltar_para": "config"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert resposta.headers["location"].startswith("/configuracoes/perfis")
    assert db.listar_perfis_monitorados(criadora.id) == []


def test_primeira_busca_sem_provedor_avisa_em_vez_de_quebrar(criadora):
    perfis_monitorados.salvar_perfil(criadora.id, "instagram", "marianax")
    token = _csrf(criadora.cliente.get("/onboarding?passo=4").text)
    resposta = criadora.cliente.post(
        "/onboarding/busca",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    destino = resposta.headers["location"]
    assert destino.startswith("/onboarding?passo=4")
    assert "erro=" in destino
    assert "SERPAPI_API_KEY" in destino
