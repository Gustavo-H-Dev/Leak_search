import asyncio
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from starlette.requests import Request

from app import db
from app.core import sites_oficiais
from app.main import app, catalogo, nova_criadora


class PaginasOficiaisTest(unittest.TestCase):
    def test_catalogo_exibe_criadora_com_sites_e_fotos_nos_registros_pais(self):
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
            }
        )
        with (
            patch("app.main.db.listar_criadoras", return_value=[{"id": 1, "nome": "Ana"}]),
            patch(
                "app.main.db.listar_sites_oficiais",
                return_value=[
                    {
                        "id": 1,
                        "criadora_id": 1,
                        "criadora": "Ana",
                        "nome": "Perfil oficial",
                        "url": "https://exemplo.com/perfil/ana",
                    }
                ],
            ),
            patch(
                "app.main.db.listar_conteudos",
                return_value=[
                    {
                        "id": 2,
                        "criadora_id": 1,
                        "arquivo": "ana.png",
                        "nome_original": "ana-original.png",
                        "criado_em": "2025-01-01",
                    }
                ],
            ),
        ):
            resposta = catalogo(request)

        corpo = resposta.body.decode()
        self.assertIn("Nova criadora", corpo)
        self.assertIn('name="site_url"', corpo)
        self.assertIn('name="arquivo"', corpo)
        self.assertIn("<h3>Ana</h3>", corpo)
        self.assertIn('name="criadora_id"', corpo)
        self.assertIn("https://exemplo.com/perfil/ana", corpo)
        self.assertIn("ana-original.png", corpo)

    def test_cadastro_de_criadora_inclui_site_opcional_quando_informado(self):
        with (
            patch("app.main.db.criar_criadora", return_value=7) as criar,
            patch("app.main.db.adicionar_site_oficial") as adicionar_site,
        ):
            resposta = asyncio.run(
                nova_criadora(" Ana ", "Site oficial", "exemplo.com/ana", None)
            )

        criar.assert_called_once_with("Ana")
        adicionar_site.assert_called_once_with(
            7, "Site oficial", "https://exemplo.com/ana"
        )
        self.assertEqual(resposta.status_code, 303)

    def test_cadastro_de_criadora_nao_exige_site(self):
        with (
            patch("app.main.db.criar_criadora", return_value=7),
            patch("app.main.db.adicionar_site_oficial") as adicionar_site,
        ):
            resposta = asyncio.run(nova_criadora("Ana", "Site oficial", "", None))

        adicionar_site.assert_not_called()
        self.assertEqual(resposta.status_code, 303)

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
