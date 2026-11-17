import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app import db
from scripts import migrar_sqlite_para_postgres


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "Defina TEST_POSTGRES_URL para integração PostgreSQL.")
class MigracaoPostgresTest(unittest.TestCase):
    def setUp(self):
        self.database_url_original = os.environ.get("DATABASE_URL")
        self.schema = f"test_migracao_{uuid.uuid4().hex}"
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema))
            )
        os.environ["DATABASE_URL"] = make_conninfo(
            POSTGRES_URL,
            options=f"-c search_path={self.schema}",
        )
        self.diretorio = TemporaryDirectory()
        self.caminho_sqlite = Path(self.diretorio.name) / "catalogo.db"
        self.caminho_db_original = db.CAMINHO_DB
        os.environ.pop("DATABASE_URL", None)
        db.CAMINHO_DB = self.caminho_sqlite
        db.iniciar_banco()
        db.criar_criadora("Ana")
        self.criadora_id = db.listar_criadoras()[0]["id"]
        db.adicionar_site_oficial(
            self.criadora_id,
            "Perfil oficial",
            "https://exemplo.com/perfil/ana",
        )
        self.conteudo_id = db.adicionar_conteudo(
            self.criadora_id,
            "foto",
            "ana.png",
            "ana.png",
            ["phash-teste"],
        )
        db.criar_admin_se_ausente(
            "Admin",
            "admin@example.com",
            "hash-de-senha",
        )
        usuario = db.obter_usuario_por_login("admin")
        db.salvar_token_auth(
            "token-hash",
            usuario["id"],
            "redefinir_senha",
            "2099-01-01T00:00:00+00:00",
        )
        db.criar_sessao(
            "session-hash",
            usuario["id"],
            "2099-01-01T00:00:00+00:00",
        )
        self.segredo_sessao = db.obter_segredo_sessao()
        os.environ["DATABASE_URL"] = make_conninfo(
            POSTGRES_URL,
            options=f"-c search_path={self.schema}",
        )

    def tearDown(self):
        db.CAMINHO_DB = self.caminho_db_original
        self.diretorio.cleanup()
        if self.database_url_original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.database_url_original
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
            )

    def test_migra_dados_e_mantem_ids_e_sequencias(self):
        db.iniciar_banco()
        migrar_sqlite_para_postgres._validar_destino_vazio()
        migrar_sqlite_para_postgres._copiar_banco(self.caminho_sqlite)
        migrar_sqlite_para_postgres._ajustar_sequencias()

        self.assertEqual(db.listar_criadoras()[0]["nome"], "Ana")
        self.assertEqual(
            db.listar_sites_oficiais()[0]["url"],
            "https://exemplo.com/perfil/ana",
        )
        self.assertEqual(db.listar_hashes()[0]["phash"], "phash-teste")
        self.assertEqual(
            db.obter_usuario_por_id(1)["username"],
            "Admin",
        )
        self.assertEqual(
            db.obter_usuario_da_sessao("session-hash")["username"],
            "Admin",
        )
        self.assertEqual(db.obter_segredo_sessao(), self.segredo_sessao)

        novo_id = db.criar_usuario("Outro", "outro@example.com", "hash")
        self.assertGreater(novo_id, 1)
        self.assertIsNotNone(db.obter_usuario_por_login("ADMIN"))

    def test_destino_com_dados_recusa_migracao(self):
        db.iniciar_banco()
        db.criar_criadora("Já existe")

        with self.assertRaisesRegex(RuntimeError, "já contém registros"):
            migrar_sqlite_para_postgres._validar_destino_vazio()


if __name__ == "__main__":
    unittest.main()
