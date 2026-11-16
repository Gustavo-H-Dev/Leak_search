from io import BytesIO
import os
import unittest
from unittest.mock import patch

import httpx
from PIL import Image
from starlette.requests import Request

from app.core import serpapi
from app.main import app, buscar_instagram, buscar_serpapi


class SerpApiTest(unittest.TestCase):
    def test_pesquisar_sem_chave_retorna_erro_de_configuracao(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("app.core.serpapi.httpx.get") as get,
        ):
            with self.assertRaises(serpapi.SerpApiNaoConfigurada):
                serpapi.pesquisar("google_images", q="teste")
        get.assert_not_called()

    def test_pesquisar_envia_engine_e_parametros_sem_expor_chave_no_resultado(self):
        resposta = httpx.Response(
            200,
            json={"images_results": []},
            request=httpx.Request("GET", "https://serpapi.com/search.json"),
        )
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "segredo"}),
            patch("app.core.serpapi.httpx.get", return_value=resposta) as get,
        ):
            dados = serpapi.pesquisar("google_images", q="teste")

        self.assertEqual(dados, {"images_results": []})
        self.assertEqual(
            get.call_args.kwargs["params"],
            {
                "engine": "google_images",
                "api_key": "segredo",
                "q": "teste",
            },
        )

    def test_normaliza_resultados_e_descarta_links_nao_web(self):
        dados = {
            "visual_matches": [
                {
                    "title": "Resultado",
                    "link": "https://example.com/post",
                    "source": "Example",
                    "thumbnail": "https://example.com/thumb.jpg",
                },
                {"title": "Link inseguro", "link": "javascript:alert(1)"},
            ]
        }

        resultados = serpapi.normalizar_resultados(dados, "google_lens")

        self.assertEqual(len(resultados), 1)
        self.assertEqual(resultados[0]["titulo"], "Resultado")
        self.assertEqual(resultados[0]["link"], "https://example.com/post")

    def test_rejeita_url_de_imagem_sem_http(self):
        self.assertFalse(serpapi.validar_url_imagem("file:///etc/passwd"))
        self.assertFalse(serpapi.validar_url_imagem("javascript:alert(1)"))
        self.assertTrue(serpapi.validar_url_imagem("https://example.com/image.jpg"))

    def test_valida_usuario_instagram(self):
        self.assertEqual(serpapi.validar_usuario_instagram("@nome.exemplo_1"), "nome.exemplo_1")
        with self.assertRaises(ValueError):
            serpapi.validar_usuario_instagram("https://instagram.com/nome")

    def test_normaliza_perfil_e_posts_do_instagram(self):
        perfil, posts = serpapi.normalizar_perfil_instagram(
            {
                "profile_results": {
                    "username": "nome.exemplo",
                    "full_name": "Nome Exemplo",
                    "biography": "Bio",
                    "followers": 123,
                    "following": 45,
                    "is_verified": True,
                    "posts": [
                        {
                            "shortcode": "AbC123",
                            "media_captions": ["Legenda pública"],
                            "product_type": "feed",
                        },
                        {"shortcode": "invalid/shortcode"},
                    ],
                }
            }
        )

        self.assertEqual(perfil["seguidores"], 123)
        self.assertEqual(perfil["link"], "https://www.instagram.com/nome.exemplo/")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["titulo"], "Legenda pública")
        self.assertEqual(posts[0]["link"], "https://www.instagram.com/p/AbC123/")

    def test_normaliza_posts_populares_e_descarta_links_invalidos(self):
        posts = serpapi.normalizar_instagram_popular(
            {
                "popular_results": [
                    {
                        "caption": "Post popular",
                        "link": "https://www.instagram.com/p/AbC123/",
                        "user": {
                            "username": "perfil",
                            "link": "https://www.instagram.com/perfil/",
                        },
                        "play_count": 1234,
                    },
                    {"link": "javascript:alert(1)"},
                ]
            }
        )

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["usuario"], "perfil")
        self.assertEqual(posts[0]["reproducoes"], "1234")

    def test_prepara_png_para_limite_e_formato_do_upload(self):
        buffer = BytesIO()
        Image.new("RGB", (20, 20), "#cc5599").save(buffer, format="PNG")

        imagem = serpapi._otimizar_imagem(buffer.getvalue())

        self.assertLessEqual(len(imagem), serpapi.TAMANHO_MAXIMO_UPLOAD)
        with Image.open(BytesIO(imagem)) as preparada:
            self.assertEqual(preparada.format, "JPEG")

    def test_google_lens_faz_upload_e_depois_uma_busca(self):
        imagem = BytesIO()
        Image.new("RGB", (20, 20), "#cc5599").save(imagem, format="PNG")
        upload = httpx.Response(
            200,
            json={"image_id": "id-temporario"},
            request=httpx.Request("POST", serpapi.UPLOAD_URL),
        )
        busca = httpx.Response(
            200,
            json={"visual_matches": []},
            request=httpx.Request("GET", serpapi.API_URL),
        )
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch("app.core.serpapi.httpx.post", return_value=upload) as post,
            patch("app.core.serpapi.httpx.get", return_value=busca) as get,
        ):
            resultado = serpapi.pesquisar_imagem(imagem.getvalue())

        self.assertEqual(resultado, {"visual_matches": []})
        self.assertEqual(post.call_args.kwargs["data"], {"api_key": "chave-de-teste"})
        self.assertEqual(get.call_args.kwargs["params"]["engine"], "google_lens")
        self.assertEqual(get.call_args.kwargs["params"]["image_id"], "id-temporario")


class SerpApiRotaTest(unittest.IsolatedAsyncioTestCase):
    def criar_request(self, path):
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [],
                "query_string": b"",
                "server": ("test", 80),
                "client": ("test", 1),
                "scheme": "http",
                "http_version": "1.1",
                "root_path": "",
                "app": app,
            }
        )

    async def test_google_images_roda_so_o_engine_selecionado(self):
        resposta = {
            "images_results": [
                {
                    "title": "Resultado",
                    "link": "https://example.com/image",
                    "related_content_id": "related-123",
                }
            ]
        }

        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch("app.main.db.listar_conteudos", return_value=[]),
            patch("app.main.serpapi.pesquisar", return_value=resposta) as pesquisar,
        ):
            resultado = await buscar_serpapi(
                self.criar_request("/buscar-serpapi"),
                "google_images",
                "on",
                "nome pesquisado",
                0,
                "",
                "",
            )

        pesquisar.assert_called_once_with(
            "google_images", q="nome pesquisado", gl="br", hl="pt"
        )
        corpo = resultado.body.decode()
        self.assertIn("Resultado", corpo)
        self.assertIn("related-123", corpo)

    async def test_instagram_profile_envia_apenas_profile_id(self):
        resposta = {
            "profile_results": {
                "username": "exemplo",
                "full_name": "Perfil Exemplo",
                "followers": 120,
                "following": 30,
                "is_private": False,
                "is_verified": False,
                "posts": [],
            }
        }
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch("app.main.serpapi.pesquisar", return_value=resposta) as pesquisar,
        ):
            resultado = await buscar_instagram(
                self.criar_request("/buscar-instagram"),
                "instagram_profile",
                "on",
                "@exemplo",
                "",
            )

        pesquisar.assert_called_once_with("instagram_profile", profile_id="exemplo")
        corpo = resultado.body.decode()
        self.assertIn("Perfil Exemplo", corpo)
        self.assertIn("120", corpo)

    async def test_instagram_popular_envia_apenas_keyword(self):
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch(
                "app.main.serpapi.pesquisar",
                return_value={"popular_results": []},
            ) as pesquisar,
        ):
            await buscar_instagram(
                self.criar_request("/buscar-instagram"),
                "instagram_popular",
                "on",
                "",
                "campanha pública",
            )

        pesquisar.assert_called_once_with(
            "instagram_popular", keyword="campanha pública"
        )


if __name__ == "__main__":
    unittest.main()
