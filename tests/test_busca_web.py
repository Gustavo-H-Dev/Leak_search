import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from starlette.requests import Request

from app.core import busca_web
from app.main import app, buscar_google_na_web


class BuscarNaWebTest(unittest.TestCase):
    def test_deduplica_resultados_e_descarta_links_nao_http(self):
        pagina = SimpleNamespace(
            url="https://example.com/post/1",
            page_title="Página pública",
            full_matching_images=[],
            partial_matching_images=[
                SimpleNamespace(url="https://cdn.example.com/parcial.png")
            ],
        )
        resposta = SimpleNamespace(
            error=SimpleNamespace(message=""),
            web_detection=SimpleNamespace(
                pages_with_matching_images=[
                    pagina,
                    pagina,
                    SimpleNamespace(
                        url="javascript:alert(1)",
                        page_title="Inválida",
                        full_matching_images=[],
                        partial_matching_images=[],
                    ),
                ],
                full_matching_images=[
                    SimpleNamespace(url="https://cdn.example.com/image.png")
                ],
                partial_matching_images=[],
                visually_similar_images=[
                    SimpleNamespace(url="https://cdn.example.com/image.png"),
                    SimpleNamespace(url="javascript:alert(1)"),
                ],
            ),
        )
        cliente = SimpleNamespace(web_detection=lambda **kwargs: resposta)

        with patch(
            "app.core.busca_web.vision.ImageAnnotatorClient",
            return_value=cliente,
        ):
            paginas, imagens = busca_web.buscar_na_web(b"conteudo de teste")

        self.assertEqual(
            paginas,
            [
                {
                    "url": "https://example.com/post/1",
                    "titulo": "Página pública",
                    "miniatura": "https://cdn.example.com/parcial.png",
                }
            ],
        )
        self.assertEqual(
            imagens,
            [
                {
                    "url": "https://cdn.example.com/image.png",
                    "tipo": "Correspondência exata",
                }
            ],
        )

    def test_propagates_erro_de_resposta_da_api(self):
        resposta = SimpleNamespace(error=SimpleNamespace(message="Falha da API"))
        cliente = SimpleNamespace(web_detection=lambda **kwargs: resposta)

        with patch(
            "app.core.busca_web.vision.ImageAnnotatorClient",
            return_value=cliente,
        ):
            with self.assertRaises(busca_web.ErroBuscaWeb):
                busca_web.buscar_na_web(b"conteudo de teste")


class BuscarNaWebRotaTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        usuario = {
            "id": 1,
            "username": "admin",
            "role": "admin",
            "public_name": "Admin",
            "public_handle": "admin",
        }
        for alvo, retorno in {
            "obter_usuario_por_id": usuario,
            "listar_usuarios_com_perfil_busca": [],
            "listar_fotos_busca": [],
            "listar_sites_vazados": [],
            "listar_sites_oficiais_usuario": [],
            "obter_snapshot_busca": None,
            "salvar_snapshot_busca": None,
        }.items():
            patcher = patch(f"app.main.db.{alvo}", return_value=retorno)
            patcher.start()
            self.addCleanup(patcher.stop)

    def criar_request(self):
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/buscar-web",
                "headers": [],
                "query_string": b"",
                "server": ("test", 80),
                "client": ("test", 1),
                "scheme": "http",
                "http_version": "1.1",
                "root_path": "",
                "app": app,
                "state": {"user": {"id": 1, "username": "admin", "role": "admin"}},
            }
        )

    async def test_bloqueia_busca_sem_consentimento(self):
        with (
            patch("app.main.db.listar_conteudos", return_value=[]),
            patch("app.main.busca_web.buscar_na_web") as buscar,
        ):
            resposta = await buscar_google_na_web(self.criar_request(), 1, "")

        self.assertIn("Confirme o envio", resposta.body.decode())
        buscar.assert_not_called()

    async def test_nao_executa_provedor_de_imagem_nao_integrado(self):
        with (
            patch("app.main.db.listar_conteudos", return_value=[]),
            patch("app.main.db.obter_conteudo") as obter_conteudo,
        ):
            resposta = await buscar_google_na_web(
                self.criar_request(),
                conteudo_id=1,
                consentimento="on",
                provedor="tineye",
            )

        self.assertIn("ainda não está integrado", resposta.body.decode())
        obter_conteudo.assert_not_called()

    async def test_envia_imagem_do_catalogo_e_manda_paginas_para_valida(self):
        conteudo = BytesIO()
        Image.new("RGB", (16, 16), "#cc5599").save(conteudo, format="PNG")
        dados_imagem = conteudo.getvalue()
        registro = {
            "id": 1,
            "criadora_id": 7,
            "arquivo": "catalogo.png",
            "nome_original": "catalogo.png",
        }
        pagina = {
            "url": "https://example.com/post",
            "titulo": "Página pública",
            "miniatura": "https://cdn.example.com/parcial.png",
        }

        with TemporaryDirectory() as diretorio:
            pasta = Path(diretorio)
            (pasta / registro["arquivo"]).write_bytes(dados_imagem)
            with (
                patch("app.main.db.listar_conteudos", return_value=[registro]),
                patch("app.main.db.obter_conteudo", return_value=registro),
                patch("app.main.PASTA_OFICIAIS", pasta),
                patch(
                    "app.main.busca_web.buscar_na_web", return_value=([pagina], [])
                ) as buscar,
                patch("app.main.db.listar_paginas_oficiais_com_donos", return_value=[]),
                patch("app.main.db.listar_candidatos", return_value=[]),
                patch("app.main.db.listar_hashes_oficiais", return_value=[]),
                patch(
                    "app.main.candidatos_core.calcular_hashes",
                    return_value={pagina["miniatura"]: "f" * 16},
                ),
                patch("app.main.db.adicionar_candidato") as adicionar,
            ):
                resposta = await buscar_google_na_web(
                    self.criar_request(), 1, "on"
                )

        buscar.assert_called_once_with(dados_imagem)
        adicionar.assert_called_once()
        self.assertEqual(adicionar.call_args.args[-2:], ("google_vision", "f" * 16))
        self.assertIn("foram enviados como candidatos", resposta.body.decode())


if __name__ == "__main__":
    unittest.main()
