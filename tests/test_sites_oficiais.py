import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from starlette.requests import Request

from app import db
from app.core import sites_oficiais
from app.main import app, catalogo


class PaginasOficiaisTest(unittest.TestCase):
    def test_catalogo_exibe_contas(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/catalogo",
                "headers": [],
                "query_string": b"",
                "server": ("test", 80),
                "client": ("test", 1),
                "scheme": "http",
                "http_version": "1.1",
                "root_path": "",
                "app": app,
                "session": {"csrf_token": "teste"},
                "state": {"user": {"id": 9, "role": "admin"}},
            }
        )
        with patch("app.main.db.listar_usuarios", return_value=[{
            "id": 1, "username": "ana", "email": "ana@example.com",
            "public_name": "Ana", "public_handle": "ana", "role": "cliente",
            "email_verified": 1, "created_at": "2025-01-01",
        }]):
            resposta = catalogo(request)

        corpo = resposta.body.decode()
        self.assertIn("Novo usuário", corpo)
        self.assertIn("<strong>Ana</strong>", corpo)
        self.assertIn("@ana", corpo)
        self.assertNotIn("ana@example.com", corpo)

    def test_normaliza_url_da_pagina_sem_ampliar_para_o_dominio(self):
        self.assertEqual(
            sites_oficiais.normalizar_url_pagina(
                " HTTP://WWW.Exemplo.com/perfil/ana/#publicacoes "
            ),
            "https://www.exemplo.com/perfil/ana",
        )
        self.assertEqual(
            sites_oficiais.normalizar_url_pagina("exemplo.com"),
            "https://exemplo.com/",
        )

    def test_rejeita_url_invalida(self):
        for valor in ("", "javascript:alert(1)", "exemplo..com", "site sem dominio"):
            with self.subTest(valor=valor):
                with self.assertRaises(ValueError):
                    sites_oficiais.normalizar_url_pagina(valor)

    def test_marca_somente_a_pagina_exata_e_a_criadora_correspondente(self):
        resultados = [
            {"url": "http://www.exemplo.com/perfil/ana/"},
            {"url": "https://www.exemplo.com/perfil/ana/publicacoes"},
            {"url": "https://www.exemplo.com/perfil/bia"},
            {"url": "https://www.exemplo.com/perfil/ana-falsa"},
        ]
        paginas = [
            {
                "criadora_id": 1,
                "criadora": "Ana",
                "url": "https://www.exemplo.com/perfil/ana",
            },
            {
                "criadora_id": 2,
                "criadora": "Bia",
                "url": "https://www.exemplo.com/perfil/bia",
            },
            {
                "criadora_id": None,
                "criadora": None,
                "url": "https://legado.example.com/",
            },
        ]

        marcados = sites_oficiais.marcar_resultados(resultados, paginas)
        marcados_ana = sites_oficiais.marcar_resultados(
            resultados, paginas, criadora_id=1
        )

        self.assertEqual(
            [resultado["criadoras_oficiais"] for resultado in marcados],
            [["Ana"], [], ["Bia"], []],
        )
        self.assertEqual(
            [resultado["criadoras_oficiais"] for resultado in marcados_ana],
            [["Ana"], [], [], []],
        )
        self.assertNotIn("criadoras_oficiais", resultados[0])

    def test_identifica_paginas_proprias_de_terceiros_e_de_dono_incerto(self):
        paginas = [
            {"user_id": 1, "url": "https://exemplo.com/perfil"},
            {"user_id": 2, "url": "https://exemplo.com/terceiro"},
            {"user_id": None, "url": "https://exemplo.com/legado"},
        ]

        self.assertEqual(
            sites_oficiais.proprietario_pagina_oficial(
                "http://exemplo.com/perfil/", paginas, 1
            ),
            "proprio",
        )
        self.assertEqual(
            sites_oficiais.proprietario_pagina_oficial(
                "https://exemplo.com/terceiro", paginas, 1
            ),
            "terceiro_ou_incerto",
        )
        self.assertEqual(
            sites_oficiais.proprietario_pagina_oficial(
                "https://exemplo.com/legado", paginas, 1
            ),
            "terceiro_ou_incerto",
        )
        self.assertEqual(
            sites_oficiais.proprietario_pagina_oficial(
                "https://exemplo.com/desconhecido", paginas, 1
            ),
            "nao_oficial",
        )

    def test_persiste_atualiza_e_remove_pagina_oficial(self):
        with TemporaryDirectory() as diretorio:
            caminho_db = Path(diretorio) / "catalogo.db"
            with patch.object(db, "CAMINHO_DB", caminho_db):
                db.iniciar_banco()
                criadora_id = db.criar_criadora("Ana")
                db.criar_criadora("Bia")
                criadoras = {c["nome"]: c["id"] for c in db.listar_criadoras()}
                self.assertEqual(criadora_id, criadoras["Ana"])
                pagina_id = db.adicionar_site_oficial(
                    criadoras["Ana"], "Perfil", "https://exemplo.com/ana"
                )
                db.adicionar_conteudo(
                    criadoras["Ana"], "foto", "ana.png", "ana-original.png", []
                )

                self.assertEqual(
                    db.listar_sites_oficiais()[0]["criadora"],
                    "Ana",
                )
                self.assertEqual(db.listar_conteudos()[0]["criadora_id"], criadora_id)
                self.assertTrue(
                    db.atualizar_site_oficial(
                        pagina_id,
                        criadoras["Bia"],
                        "Página Bia",
                        "https://exemplo.com/bia",
                    )
                )
                self.assertEqual(
                    db.listar_sites_oficiais()[0]["criadora"],
                    "Bia",
                )
                self.assertTrue(db.excluir_site_oficial(pagina_id))
                self.assertFalse(db.excluir_site_oficial(pagina_id))
                self.assertEqual(db.listar_sites_oficiais(), [])

    def test_migra_dominios_legados_sem_associar_ou_marcar_como_oficiais(self):
        with TemporaryDirectory() as diretorio:
            caminho_db = Path(diretorio) / "catalogo.db"
            with sqlite3.connect(caminho_db) as conn:
                conn.execute(
                    """
                    CREATE TABLE sites_oficiais (
                        id INTEGER PRIMARY KEY,
                        nome TEXT NOT NULL,
                        dominio TEXT NOT NULL UNIQUE,
                        criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO sites_oficiais (nome, dominio) VALUES (?, ?)",
                    ("Site antigo", "exemplo.com"),
                )

            with patch.object(db, "CAMINHO_DB", caminho_db):
                db.iniciar_banco()
                pagina = db.listar_sites_oficiais()[0]

                self.assertIsNone(pagina["criadora_id"])
                self.assertEqual(pagina["url"], "https://exemplo.com/")
                self.assertEqual(
                    sites_oficiais.marcar_resultados(
                        [{"url": "https://exemplo.com/"}], [pagina]
                    )[0]["criadoras_oficiais"],
                    [],
                )


if __name__ == "__main__":
    unittest.main()


def test_perfil_tiktok_marca_videos_do_mesmo_usuario_como_oficiais():
    from app.core import sites_oficiais

    paginas = [
        {"criadora_id": 1, "criadora": "Ana", "url": "https://www.tiktok.com/@Ana.x"}
    ]
    resultados = [
        {"link": "https://www.tiktok.com/@ana.x/video/123"},
        {"link": "https://www.tiktok.com/@outra/video/123"},
    ]
    marcados = sites_oficiais.marcar_resultados(resultados, paginas)
    assert [r["criadoras_oficiais"] for r in marcados] == [["Ana"], []]


def test_pagina_filha_de_site_oficial_e_oficial_mas_irma_nao():
    from app.core import sites_oficiais

    paginas = [{"user_id": 1, "url": "https://a.com/perfil"}]
    classificar = lambda url: sites_oficiais.proprietario_pagina_oficial(
        url, paginas, 1
    )
    assert classificar("https://a.com/perfil/posts/9") == "proprio"
    assert classificar("https://a.com/perfil-falso") == "nao_oficial"
    assert classificar("https://b.com/perfil/posts/9") == "nao_oficial"


def test_pagina_de_listagem_detecta_busca_e_tag_mas_nao_video():
    from app.core import sites_oficiais

    f = sites_oficiais.pagina_de_listagem
    assert f("https://www.tiktok.com/search?q=ana")
    assert f("https://www.tiktok.com/pt-BR/tag/ana")
    assert f("https://www.youtube.com/results?search_query=ana")
    assert not f("https://www.tiktok.com/@ana/video/1")
    assert not f("https://www.youtube.com/watch?v=abc")


class InstagramOficialTests(unittest.TestCase):
    def test_identifica_usuario_e_post(self):
        self.assertEqual(sites_oficiais.usuario_instagram("https://www.instagram.com/ana/"), "ana")
        self.assertEqual(sites_oficiais.usuario_instagram("https://www.instagram.com/p/AbC/"), "")
        self.assertEqual(sites_oficiais.shortcode_instagram("https://www.instagram.com/p/AbC/"), "AbC")
        self.assertEqual(sites_oficiais.shortcode_instagram("https://instagram.com/ana/reel/XyZ/"), "XyZ")
        self.assertEqual(sites_oficiais.shortcode_instagram("https://www.instagram.com/ana/"), "")
