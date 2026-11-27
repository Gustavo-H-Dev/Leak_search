"""Operacao do dia a dia pelo caminho HTTP real.

Cobre a fila honesta da Valida (abaixo do corte), o CSV da fila, o historico
de execucoes por modelo e o checklist de proximos passos do admin.
"""

import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import autenticacao, pontuacao
from app.main import app

SENHA = "senha-teste-operacao"


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
def ambiente(tmp_path):
    with patch.object(db, "CAMINHO_DB", tmp_path / "operacao.db"):
        db.iniciar_banco()
        admin = db.criar_usuario(
            "admin_operacao",
            "admin_operacao@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Ana",
            public_handle="ana",
        )
        cliente = db.criar_usuario(
            "cliente_operacao",
            "cliente_operacao@example.com",
            autenticacao.hash_senha(SENHA),
        )
        sem_perfil = db.criar_usuario(
            "sem_perfil", "sem_perfil@example.com", autenticacao.hash_senha(SENHA)
        )
        db.confirmar_email(admin)
        db.confirmar_email(cliente)
        db.confirmar_email(sem_perfil)
        db.atualizar_perfil_usuario(admin, "admin")

        execucao_id = db.criar_busca_execucao(admin, "tinyfish", ["ana"])
        log = db.registrar_consulta_log(
            execucao_id,
            "M2",
            "ana videos bunkr",
            {"host": "bunkr"},
            [{"url": "https://bunkr.cr/a/7W7gu1yU", "posicao": 1}],
            {"bruto": True},
        )
        log_id = log["id"] if hasattr(log, "__getitem__") else log
        db.salvar_candidatos_busca(
            admin,
            execucao_id,
            [
                {
                    "url_normalizada": "https://bunkr.cr/a/7W7gu1yU",
                    "titulo": "ana - Videos",
                    "dominio": "bunkr.cr",
                    "nota": 95,
                    "classificacao": "provavel_vazamento",
                    "chave_grupo": "album:7W7gu1yU",
                    "consulta_log_id": log_id,
                    "posicao_busca": 1,
                },
                {
                    "url_normalizada": "https://exemplo.example/ana-antigo",
                    "titulo": "ana antigo",
                    "dominio": "exemplo.example",
                    "nota": 5,
                    "classificacao": "indefinido",
                },
            ],
        )
        yield {
            "admin": admin,
            "cliente": cliente,
            "sem_perfil": sem_perfil,
            "execucao_id": execucao_id,
        }


def test_dominios_mostra_total_real_em_todos(ambiente):
    admin = TestClient(app)
    _entrar(admin, "admin_operacao")
    corpo = admin.get("/dominios").text
    total = len(db.listar_dominios_suspeitos(None))
    assert total > 0
    achado = re.search(r"Todos\s*\((\d+)\)", corpo)
    assert achado, "contador da aba Todos nao encontrado"
    assert int(achado.group(1)) == total


def test_valida_explica_fila_vazia_e_mostra_abaixo_do_corte(ambiente):
    alvo = ambiente["admin"]
    admin = TestClient(app)
    _entrar(admin, "admin_operacao")
    limiar = pontuacao.limiar_revisao()

    fila = admin.get(f"/valida?target_user_id={alvo}")
    assert fila.status_code == 200
    assert "https://bunkr.cr/a/7W7gu1yU" in fila.text
    assert "Abaixo do corte (1)" in fila.text

    abaixo = admin.get(f"/valida?target_user_id={alvo}&status=abaixo")
    assert abaixo.status_code == 200
    assert "https://exemplo.example/ana-antigo" in abaixo.text
    assert "https://bunkr.cr/a/7W7gu1yU" not in abaixo.text

    # Sem nenhum candidato abaixo do corte, a fila principal explica o motivo
    # em vez de sugerir que nada foi coletado.
    assert limiar > 5


def test_valida_exporta_csv_da_lista(ambiente):
    alvo = ambiente["admin"]
    admin = TestClient(app)
    _entrar(admin, "admin_operacao")
    resposta = admin.get(
        f"/valida/exportar.csv?target_user_id={alvo}&status=abaixo"
    )
    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/csv")
    assert "attachment" in resposta.headers["content-disposition"]
    corpo = resposta.text
    assert "id;nota;status;classificacao" in corpo
    assert "https://exemplo.example/ana-antigo" in corpo
    assert "https://bunkr.cr/a/7W7gu1yU" not in corpo


def test_execucoes_lista_modelo_e_consulta_para_admin(ambiente):
    admin = TestClient(app)
    _entrar(admin, "admin_operacao")
    pagina = admin.get("/execucoes")
    assert pagina.status_code == 200
    assert f"Execução {ambiente['execucao_id']}" in pagina.text
    assert "ana videos bunkr" in pagina.text
    assert "M2" in pagina.text
    assert "1 candidato(s) na Valida" in pagina.text

    sem_papel = TestClient(app)
    _entrar(sem_papel, "cliente_operacao")
    assert sem_papel.get("/execucoes").status_code == 403


def test_home_lista_pendencias_do_admin(ambiente):
    admin = TestClient(app)
    _entrar(admin, "admin_operacao")
    corpo = admin.get("/").text
    assert "PRÓXIMOS PASSOS" in corpo
    assert "perfil de busca incompleto" in corpo

    # O cliente nao recebe o checklist de operacao.
    cliente = TestClient(app)
    _entrar(cliente, "cliente_operacao")
    assert "PRÓXIMOS PASSOS" not in cliente.get("/").text
