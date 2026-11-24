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
        with db.conexao() as conn:
            conn.execute(
                "UPDATE criadoras SET user_id = ?, email = ?, handle = ? WHERE id = ?",
                (usuario["id"], "admin@example.com", "admin", self.criadora_id),
            )
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
        self.assertEqual(db.obter_criadora(self.criadora_id)["user_id"], 1)
        self.assertEqual(db.remover_criadoras_sem_conta(), [])
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

    def test_remocao_de_perfis_antigos_e_nome_repetido(self):
        db.iniciar_banco()
        primeiro = db.criar_usuario("ana", "ana@example.com", "hash", "Ana", "ana")
        segundo = db.criar_usuario("bia", "bia@example.com", "hash", "Bia", "bia")
        primeira = db.criar_criadora_com_contato("Ana", "ana@example.com")
        segunda = db.criar_criadora_com_contato("Bia", "bia@example.com")
        antiga = db.criar_criadora_com_contato("Antiga", "antiga@example.com")
        with db.conexao() as conn:
            conn.execute("UPDATE criadoras SET user_id = ? WHERE id = ?", (primeiro, primeira))
            conn.execute("UPDATE criadoras SET user_id = ? WHERE id = ?", (segundo, segunda))
        db.adicionar_conteudo(antiga, "foto", "antiga.png", "antiga.png", ["a" * 16])
        db.atualizar_perfil_busca(segundo, "Ana", "nova_bia")
        self.assertEqual(db.obter_usuario_por_id(segundo)["public_name"], "Ana")
        self.assertEqual(db.remover_criadoras_sem_conta(), ["antiga.png"])
        self.assertIsNone(db.obter_criadora(antiga))
        self.assertIsNotNone(db.obter_criadora(segunda))


class MigracaoDadosBuscaPessoalTest(unittest.TestCase):
    def test_copia_perfil_fotos_sites_e_relatos_para_o_banco_destino(self):
        with TemporaryDirectory() as diretorio:
            caminho_origem = Path(diretorio) / "origem.db"
            caminho_destino = Path(diretorio) / "destino.db"
            caminho_db_original = db.CAMINHO_DB
            try:
                db.CAMINHO_DB = caminho_origem
                db.iniciar_banco()
                user_id = db.criar_usuario(
                    "cliente", "cliente@example.com", "password-hash"
                )
                db.atualizar_perfil_busca(user_id, "BlackDragon", "user875s")
                criadora_id = db.criar_criadora_com_contato(
                    "BlackDragon", "cliente@example.com", "user875s"
                )
                with db.conexao() as conn:
                    conn.execute(
                        "UPDATE criadoras SET user_id = ? WHERE id = ?",
                        (user_id, criadora_id),
                    )
                db.adicionar_conteudo(
                    criadora_id, "foto", "catalogo.png", "catalogo.png", ["a" * 16]
                )
                db.adicionar_foto_busca(user_id, "foto.png", "foto original.png")
                db.adicionar_site_oficial_usuario(
                    user_id,
                    user_id,
                    "Perfil oficial",
                    "https://example.com/perfil",
                )
                db.adicionar_site_vazado(
                    user_id,
                    user_id,
                    "https://example.com/vazado",
                    "Resultado",
                    "Example",
                    "Descrição",
                    "https://example.com/thumb.jpg",
                    "google_images",
                    {"date_resultado": "hoje"},
                )
                db.adicionar_candidato(
                    user_id,
                    "https://example.com/image-page",
                    "Imagem candidata",
                    "",
                    "",
                    "",
                    "google_images",
                    "b" * 16,
                )
                db.adicionar_hash_oficial(
                    user_id, "https://example.com/perfil", "c" * 16
                )

                db.CAMINHO_DB = caminho_destino
                db.iniciar_banco()
                migrar_sqlite_para_postgres._copiar_banco(caminho_origem)

                usuario = db.obter_usuario_por_id(user_id)
                self.assertEqual(usuario["public_name"], "BlackDragon")
                criadora = db.obter_criadora(criadora_id)
                self.assertEqual(criadora["user_id"], user_id)
                self.assertEqual(criadora["handle"], "user875s")
                self.assertEqual(db.remover_criadoras_sem_conta(), [])
                self.assertEqual(len(db.listar_hashes_criadora_do_usuario(user_id)), 1)
                self.assertEqual(
                    db.listar_fotos_busca(user_id)[0]["nome_original"],
                    "foto original.png",
                )
                self.assertEqual(
                    db.listar_sites_oficiais_usuario(user_id)[0]["url"],
                    "https://example.com/perfil",
                )
                self.assertEqual(
                    db.listar_sites_vazados(user_id)[0]["url"],
                    "https://example.com/vazado",
                )
                self.assertEqual(
                    db.listar_candidatos(user_id)[0]["title"],
                    "Imagem candidata",
                )
                self.assertEqual(
                    db.listar_hashes_oficiais(user_id)[0]["phash"], "c" * 16
                )
            finally:
                db.CAMINHO_DB = caminho_db_original


if __name__ == "__main__":
    unittest.main()
