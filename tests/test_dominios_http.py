"""Revisao de dominios pelo caminho HTTP real: /dominios e /valida.

Usa TestClient do FastAPI com login de verdade (sessao + CSRF), o mesmo papel
que ja faz a revisao em /valida. Nenhuma chamada direta ao endpoint: e o
formulario que o navegador enviaria.
"""

import os
import re
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import (
    autenticacao,
    consultas_vazamento,
    dominios_suspeitos,
    pontuacao,
)
from app.main import app

SENHA = "senha-teste-dominios"


def _csrf(html: str) -> str:
    achado = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert achado, "token CSRF nao encontrado na pagina"
    return achado.group(1)


def _entrar(cliente: TestClient, usuario: str) -> None:
    pagina = cliente.get("/login")
    token = _csrf(pagina.text)
    resposta = cliente.post(
        "/login",
        data={"csrf_token": token, "username": usuario, "senha": SENHA, "next": "/"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303, resposta.text


def _post(cliente: TestClient, caminho: str, dados: dict):
    token = _csrf(cliente.get("/dominios").text)
    return cliente.post(
        caminho, data={"csrf_token": token, **dados}, follow_redirects=False
    )


def _sugerir(dominio: str, nota: int, sinais=("sinal de teste",), aparicoes=2):
    db.registrar_sugestao_dominio(
        dominio,
        nota,
        list(sinais),
        f"https://{dominio}/a/7W7gu1yU",
        {"aparicoes": aparicoes, "execucoes": 2, "criadoras": 1},
    )


def _dominio(prefixo: str) -> str:
    return f"{prefixo}-{uuid.uuid4().hex[:8]}.example"


@pytest.fixture()
def ambiente(tmp_path):
    with patch.object(db, "CAMINHO_DB", tmp_path / "dominios_http.db"):
        db.iniciar_banco()
        admin = db.criar_usuario(
            "admin_http",
            "admin_http@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Ana",
            public_handle="ana",
        )
        cliente = db.criar_usuario(
            "cliente_http",
            "cliente_http@example.com",
            autenticacao.hash_senha(SENHA),
        )
        db.confirmar_email(admin)
        db.confirmar_email(cliente)
        db.atualizar_perfil_usuario(admin, "admin")
        yield {"admin": admin, "cliente": cliente}


def test_pagina_lista_sugeridos_por_nota_e_barra_nao_admin(ambiente):
    alta = _dominio("nota-alta")
    baixa = _dominio("nota-baixa")
    _sugerir(baixa, 55, sinais=["sinal da baixa"], aparicoes=2)
    _sugerir(alta, 85, sinais=["sinal da alta"], aparicoes=3)

    admin = TestClient(app)
    _entrar(admin, "admin_http")
    resposta = admin.get("/dominios")
    assert resposta.status_code == 200
    corpo = resposta.text
    assert alta in corpo and baixa in corpo
    assert corpo.index(alta) < corpo.index(baixa)
    assert "sinal da alta" in corpo
    assert f"https://{alta}/a/7W7gu1yU" in corpo
    assert f"{alta}" in corpo and "3 aparições" in corpo
    # O formulario que o navegador usa esta na propria pagina.
    assert f'action="/dominios/{alta}/status"' in corpo
    assert 'name="tipo"' in corpo and "Aprovar" in corpo

    sem_papel = TestClient(app)
    _entrar(sem_papel, "cliente_http")
    assert sem_papel.get("/dominios").status_code == 403


def test_aprovar_rejeitar_e_aprovar_sem_tipo(ambiente):
    aprovado = _dominio("aprovar")
    rejeitado = _dominio("rejeitar")
    sem_tipo = _dominio("sem-tipo")
    for dominio in (aprovado, rejeitado, sem_tipo):
        _sugerir(dominio, 70)

    cliente = TestClient(app)
    _entrar(cliente, "admin_http")

    resposta = _post(
        cliente,
        f"/dominios/{aprovado}/status",
        {"acao": "aprovar", "tipo": "hospedagem_arquivos"},
    )
    assert resposta.status_code == 303
    registro = db.obter_dominio_suspeito(aprovado)
    assert registro["status"] == "aprovado"
    assert registro["tipo"] == "hospedagem_arquivos"
    assert registro["origem"] == "revisao"

    resposta = _post(
        cliente, f"/dominios/{rejeitado}/status", {"acao": "rejeitar"}
    )
    assert resposta.status_code == 303
    registro = db.obter_dominio_suspeito(rejeitado)
    assert registro["status"] == "rejeitado"
    assert registro["origem"] == "revisao"

    # Aprovar sem tipo nao pode cair num padrao silencioso.
    resposta = _post(
        cliente, f"/dominios/{sem_tipo}/status", {"acao": "aprovar"}
    )
    assert resposta.status_code == 400
    registro = db.obter_dominio_suspeito(sem_tipo)
    assert registro["status"] == "sugerido"
    assert registro["origem"] == "auto"


def test_aprovado_pontua_e_entra_no_m2_sugerido_e_rejeitado_nao(ambiente):
    aprovado = _dominio("host-ok")
    indice = _dominio("host-indice")
    rejeitado = _dominio("host-rejeitado")
    sugerido = _dominio("host-sugerido")
    for dominio in (aprovado, indice, rejeitado, sugerido):
        _sugerir(dominio, 75)

    cliente = TestClient(app)
    _entrar(cliente, "admin_http")
    assert _post(
        cliente,
        f"/dominios/{aprovado}/status",
        {"acao": "aprovar", "tipo": "hospedagem_arquivos"},
    ).status_code == 303
    assert _post(
        cliente,
        f"/dominios/{indice}/status",
        {"acao": "aprovar", "tipo": "indice_albuns"},
    ).status_code == 303
    assert _post(
        cliente,
        f"/dominios/{rejeitado}/status",
        {"acao": "aprovar", "tipo": "indice_albuns"},
    ).status_code == 303
    assert _post(
        cliente, f"/dominios/{rejeitado}/status", {"acao": "rejeitar"}
    ).status_code == 303

    # Pontuacao: so o aprovado soma o sinal de dominio de vazamento.
    ok = pontuacao.avaliar(
        f"https://{aprovado}/a/1", "Ana vazado", "", arrobas=["ana"]
    )
    assert any(aprovado in motivo for motivo in ok["motivos"])

    rejeitado_avaliado = pontuacao.avaliar(
        f"https://{rejeitado}/a/1", "Ana vazado", "", arrobas=["ana"]
    )
    assert not any(rejeitado in motivo for motivo in rejeitado_avaliado["motivos"])
    assert rejeitado_avaliado["nota"] == ok["nota"] - 40

    apenas_sugerido = pontuacao.avaliar(
        f"https://{sugerido}/a/1", "Ana vazado", "", arrobas=["ana"]
    )
    assert not any(sugerido in motivo for motivo in apenas_sugerido["motivos"])

    # M2: o aprovado de hospedagem_arquivos vira consulta; os outros nao.
    planos = consultas_vazamento.gerar_consultas_priorizadas(
        ["Morph1ne2.0", "Morph1neee_"], ["vazado"], [], limite=60
    )
    consultas_m2 = [plano["consulta"] for plano in planos if plano["modelo"] == "M2"]
    assert f"morph1ne videos {aprovado.split('.')[0]}" in consultas_m2
    # O tipo indice_albuns tambem entra no M2.
    assert f"morph1ne videos {indice.split('.')[0]}" in consultas_m2
    for dominio in (rejeitado, sugerido):
        termo = dominio.split(".")[0]
        assert not any(termo in consulta for consulta in consultas_m2), dominio


def test_feedback_pela_revisao_conta_e_marca_revisar(ambiente):
    dominio = _dominio("feedback-http")
    _sugerir(dominio, 80)
    cliente = TestClient(app)
    _entrar(cliente, "admin_http")
    assert _post(
        cliente,
        f"/dominios/{dominio}/status",
        {"acao": "aprovar", "tipo": "agregador_vazamento"},
    ).status_code == 303

    candidatos = []
    for indice in range(3):
        db.adicionar_candidato(
            ambiente["admin"],
            f"https://{dominio}/a/{indice}",
            "Ana vazado",
            "",
            "",
            "",
            "serp",
            "",
        )
    candidatos = [
        linha["id"]
        for linha in db.listar_candidatos(ambiente["admin"])
        if dominio in linha["url"]
    ]
    assert len(candidatos) == 3

    token = _csrf(cliente.get("/revisao").text)
    with patch.dict(os.environ, {"DOMINIOS_LIMITE_REVISOES": "2"}):
        for candidate_id, status in zip(candidatos, ("leaked", "not_me", "not_me")):
            resposta = cliente.post(
                "/revisao/acao",
                data={
                    "csrf_token": token,
                    "ids": [candidate_id],
                    "status": status,
                    "target_user_id": 0,
                },
                follow_redirects=False,
            )
            assert resposta.status_code == 303

    registro = db.obter_dominio_suspeito(dominio)
    assert registro["n_confirmados"] == 1
    assert registro["n_falsos"] == 2
    assert registro["observacao"] == "revisar"
    assert registro["status"] == "aprovado"


def test_seed_no_boot_e_idempotente(ambiente):
    total = len(db.listar_dominios_suspeitos())
    ignorados = db.listar_dominios_ignorados()
    assert "bunkr.cr" in {item["dominio"] for item in db.listar_dominios_suspeitos()}
    assert len(ignorados) == len(set(ignorados))

    assert db.atualizar_status_dominio(
        "erome.com", "aprovado", tipo="agregador_vazamento"
    )

    # Rodar o boot de novo nao duplica nem rebaixa o que ja foi revisado.
    db.iniciar_banco()
    assert len(db.listar_dominios_suspeitos()) == total
    registro = db.obter_dominio_suspeito("erome.com")
    assert registro["status"] == "aprovado"
    assert registro["tipo"] == "agregador_vazamento"
    assert registro["origem"] == "revisao"
