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
