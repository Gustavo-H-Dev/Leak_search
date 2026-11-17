import re
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db
from app.core import autenticacao
from app.main import app


class AutenticacaoTest(unittest.TestCase):
    def test_usuario_novo_e_cliente_com_data_de_cadastro_no_banco(self):
        with TemporaryDirectory() as diretorio:
            with patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"):
                db.iniciar_banco()
                user_id = db.criar_usuario(
                    "cliente_teste",
                    "cliente@example.com",
                    "hash-nao-utilizado-no-teste",
                )
                usuario = next(
                    user for user in db.listar_usuarios() if user["id"] == user_id
                )

                self.assertEqual(usuario["role"], "cliente")
                self.assertTrue(usuario["created_at"])

                with db.conexao() as conn:
                    conn.execute(
                        "UPDATE users SET role = 'user' WHERE id = ?", (user_id,)
                    )
                db._migrar_usuarios()
                self.assertEqual(
                    db.obter_usuario_por_id(user_id)["role"], "cliente"
                )

    def test_pagina_e_alteracao_de_perfis_sao_restritas_a_admin(self):
        with TemporaryDirectory() as diretorio:
            with patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"):
                db.iniciar_banco()
                db.criar_admin_se_ausente(
                    "admin_teste",
                    "admin@example.com",
                    autenticacao.hash_senha("senha-admin-teste-forte"),
                )
                admin_id = db.obter_usuario_por_login("admin_teste")["id"]
                cliente_id = db.criar_usuario(
                    "cliente_teste",
                    "cliente@example.com",
                    autenticacao.hash_senha("senha-cliente-teste-forte"),
                )
                db.confirmar_email(cliente_id)

                with TestClient(app) as cliente:
                    login = cliente.get("/login")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"', login.content
                    ).group(1).decode()
                    resposta_login = cliente.post(
                        "/login",
                        data={
                            "username": "cliente_teste",
                            "senha": "senha-cliente-teste-forte",
                            "next": "/",
                            "csrf_token": token_csrf,
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(resposta_login.status_code, 303)
                    pagina_cliente = cliente.get("/")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"', pagina_cliente.content
                    ).group(1).decode()
                    pagina_restrita = cliente.get("/usuarios")
                    tentativa_alteracao = cliente.post(
                        f"/usuarios/{cliente_id}/perfil",
                        data={"role": "admin", "csrf_token": token_csrf},
                    )

                self.assertEqual(pagina_restrita.status_code, 403)
                self.assertNotIn(b'href="/usuarios"', pagina_cliente.content)
                self.assertEqual(tentativa_alteracao.status_code, 403)
                self.assertEqual(db.obter_usuario_por_id(cliente_id)["role"], "cliente")

                with TestClient(app) as admin:
                    login = admin.get("/login")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"', login.content
                    ).group(1).decode()
                    resposta_login = admin.post(
                        "/login",
                        data={
                            "username": "admin_teste",
                            "senha": "senha-admin-teste-forte",
                            "next": "/",
                            "csrf_token": token_csrf,
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(resposta_login.status_code, 303)
                    pagina_usuarios = admin.get("/usuarios")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"',
                        pagina_usuarios.content,
                    ).group(1).decode()
                    atualizado = admin.post(
                        f"/usuarios/{cliente_id}/perfil",
                        data={"role": "admin", "csrf_token": token_csrf},
                        follow_redirects=False,
                    )
                    auto_alteracao = admin.post(
                        f"/usuarios/{admin_id}/perfil",
                        data={"role": "cliente", "csrf_token": token_csrf},
                        follow_redirects=False,
                    )

                self.assertEqual(pagina_usuarios.status_code, 200)
                self.assertIn(b'href="/usuarios"', pagina_usuarios.content)
                self.assertIn(b"cliente@example.com", pagina_usuarios.content)
                self.assertIn(b"Cadastro", pagina_usuarios.content)
                self.assertEqual(atualizado.status_code, 303)
                self.assertEqual(auto_alteracao.status_code, 303)
                self.assertEqual(db.obter_usuario_por_id(cliente_id)["role"], "admin")
                self.assertEqual(
                    db.obter_usuario_por_login("admin_teste")["role"], "admin"
                )

    def test_hash_de_senha_verifica_sem_armazenar_senha_em_texto(self):
        senha = "uma-senha-forte-de-teste"
        password_hash = autenticacao.hash_senha(senha)

        self.assertNotEqual(password_hash, senha)
        self.assertTrue(autenticacao.verificar_senha(password_hash, senha))
        self.assertFalse(autenticacao.verificar_senha(password_hash, "senha errada"))

    def test_rejeita_senha_abaixo_do_minimo(self):
        with self.assertRaisesRegex(ValueError, "pelo menos 12"):
            autenticacao.hash_senha("curta")

    def test_token_auth_expira_e_so_pode_ser_usado_uma_vez(self):
        with TemporaryDirectory() as diretorio:
            caminho_db = Path(diretorio) / "auth.db"
            with patch.object(db, "CAMINHO_DB", caminho_db):
                db.iniciar_banco()
                user_id = db.criar_usuario(
                    "usuario_teste",
                    "teste@example.com",
                    "hash-nao-utilizado-no-teste",
                )
                db.salvar_token_auth(
                    "hash-token-valido",
                    user_id,
                    "confirmar_email",
                    (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                )
                db.salvar_token_auth(
                    "hash-token-expirado",
                    user_id,
                    "redefinir_senha",
                    (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                )

                self.assertEqual(
                    db.consumir_token_auth("hash-token-valido", "confirmar_email"),
                    user_id,
                )
                self.assertIsNone(
                    db.consumir_token_auth("hash-token-valido", "confirmar_email")
                )
                self.assertIsNone(
                    db.consumir_token_auth("hash-token-expirado", "redefinir_senha")
                )

    def test_home_e_login_sao_publicos_e_catalogo_exige_sessao(self):
        with TestClient(app) as client:
            home = client.get("/", follow_redirects=False)
            login = client.get("/login", follow_redirects=False)
            catalogo = client.get("/catalogo", follow_redirects=False)
            cadastro = client.get("/cadastro", follow_redirects=False)
            recuperar = client.get("/esqueci-senha", follow_redirects=False)
            csrf_ausente = client.post(
                "/login",
                data={"username": "usuario", "senha": "senha"},
                follow_redirects=False,
            )

        self.assertEqual(home.status_code, 200)
        self.assertEqual(login.status_code, 200)
        self.assertIn(b'name="csrf_token"', login.content)
        self.assertEqual(cadastro.status_code, 200)
        self.assertIn(b'name="csrf_token"', cadastro.content)
        self.assertEqual(recuperar.status_code, 200)
        self.assertEqual(csrf_ausente.status_code, 403)
        self.assertEqual(catalogo.status_code, 303)
        self.assertEqual(catalogo.headers["location"], "/login?next=/catalogo")


if __name__ == "__main__":
    unittest.main()
