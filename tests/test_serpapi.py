from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.requests import Request

from app import db
from app.core import serpapi
from app.main import (
    app,
    buscar_instagram,
    buscar_serpapi,
    obter_foto_busca,
    pagina_busca_na_web,
    perfil_busca,
    salvar_perfil_busca,
    status_serpapi,
)


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

    def test_account_api_le_saldo_sem_expor_dados_privados(self):
        resposta = httpx.Response(
            200,
            json={
                "api_key": "chave-secreta",
                "account_email": "privado@example.com",
                "account_status": "Active",
                "plan_name": "Free",
                "searches_per_month": 250,
                "plan_searches_left": 200,
                "extra_credits": 0,
                "total_searches_left": 200,
                "this_month_usage": 50,
                "plan_renewal_date": "2026-12-01",
            },
            request=httpx.Request("GET", serpapi.ACCOUNT_API_URL),
        )
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-secreta"}),
            patch("app.core.serpapi.httpx.get", return_value=resposta) as get,
        ):
            conta = serpapi.obter_status_conta()

        self.assertEqual(conta["this_month_usage"], 50)
        self.assertEqual(conta["plan_searches_left"], 200)
        self.assertNotIn("api_key", conta)
        self.assertNotIn("account_email", conta)
        get.assert_called_once_with(
            serpapi.ACCOUNT_API_URL,
            params={"api_key": "chave-secreta"},
            timeout=serpapi.TIMEOUT,
        )

    def test_account_api_sem_chave_nao_faz_requisicao(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("app.core.serpapi.httpx.get") as get,
        ):
            with self.assertRaises(serpapi.SerpApiNaoConfigurada):
                serpapi.obter_status_conta()
        get.assert_not_called()

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
    def setUp(self):
        self.patchers_snapshot = [
            patch("app.main.db.obter_snapshot_busca", return_value=None),
            patch("app.main.db.salvar_snapshot_busca", return_value=None),
        ]
        for patcher in self.patchers_snapshot:
            patcher.start()
            self.addCleanup(patcher.stop)

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
                "state": {
                    "user": {
                        "id": 1,
                        "username": "cliente",
                        "role": "cliente",
                    }
                },
            }
        )

    async def test_google_images_roda_so_o_engine_selecionado_e_envia_para_valida(self):
        resposta = {
            "images_results": [
                {
                    "title": "Resultado",
                    "link": "https://example.com/image",
                    "related_content_id": "related-123",
                },
                {"title": "Oficial", "link": "https://example.com/perfil/post"},
                {"title": "Repetido", "link": "https://example.com/image/"},
            ]
        }
        usuario = {
            "id": 1,
            "username": "cliente",
            "role": "cliente",
            "public_name": "BlackDragon",
            "public_handle": "user875s",
        }

        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch("app.main.db.obter_usuario_por_id", return_value=usuario),
            patch("app.main.db.listar_fotos_busca", return_value=[]),
            patch("app.main.db.listar_sites_vazados", return_value=[]),
            patch("app.main.db.listar_conteudos", return_value=[]),
            patch(
                "app.main.db.listar_paginas_oficiais_com_donos",
                return_value=[{"user_id": 1, "url": "https://example.com/perfil"}],
            ),
            patch(
                "app.main.db.listar_candidatos",
                return_value=[
                    {"url": "https://example.com/recusado", "status": "not_me"}
                ],
            ),
            patch("app.main.db.listar_hashes_oficiais", return_value=[]),
            patch("app.main.db.adicionar_candidato") as adicionar,
            patch("app.main.serpapi.pesquisar", return_value=resposta) as pesquisar,
        ):
            resultado = await buscar_serpapi(
                self.criar_request("/buscar-serpapi"),
                api="google_images",
                consentimento="on",
                foto_id=0,
                related_content_id="",
                target_user_id=1,
                image_file=None,
            )

        pesquisar.assert_called_once_with(
            "google_images", q="BlackDragon @user875s", gl="br", hl="pt", safe="off"
        )
        adicionar.assert_called_once()
        self.assertEqual(adicionar.call_args.args[:2], (1, "https://example.com/image"))
        corpo = resultado.body.decode()
        self.assertIn("related-123", corpo)
        self.assertIn("/valida?target_user_id=1", corpo)
        self.assertNotIn("VAZADO!", corpo)

    async def test_cliente_nao_pode_escolher_outro_usuario_para_busca(self):
        with self.assertRaises(HTTPException) as erro:
            await buscar_serpapi(
                self.criar_request("/buscar-serpapi"),
                api="google_images",
                consentimento="on",
                target_user_id=2,
            )

        self.assertEqual(erro.exception.status_code, 403)

    async def test_admin_pode_buscar_usuario_selecionado_com_seu_perfil(self):
        request = self.criar_request("/buscar-serpapi")
        request.state.user = {"id": 9, "role": "admin"}
        alvo = {
            "id": 2,
            "username": "cliente",
            "role": "cliente",
            "public_name": "BlackDragon",
            "public_handle": "user875s",
        }
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch("app.main.db.obter_usuario_por_id", return_value=alvo),
            patch("app.main.db.listar_fotos_busca", return_value=[]),
            patch("app.main.db.listar_sites_oficiais_usuario", return_value=[]),
            patch("app.main.db.listar_sites_vazados", return_value=[]),
            patch("app.main.db.listar_usuarios_para_busca", return_value=[]),
            patch("app.main.serpapi.pesquisar", return_value={"images_results": []}) as pesquisar,
        ):
            await buscar_serpapi(
                request,
                api="google_images",
                consentimento="on",
                foto_id=0,
                related_content_id="",
                target_user_id=2,
                image_file=None,
            )

        pesquisar.assert_called_once_with(
            "google_images", q="BlackDragon @user875s", gl="br", hl="pt", safe="off"
        )

    async def test_admin_nao_pode_usar_foto_de_outro_usuario_na_busca_visual(self):
        request = self.criar_request("/buscar-serpapi")
        request.state.user = {"id": 9, "role": "admin"}
        alvo = {
            "id": 2,
            "username": "cliente",
            "role": "cliente",
            "public_name": "BlackDragon",
            "public_handle": "user875s",
        }
        with (
            patch("app.main.db.obter_usuario_por_id", return_value=alvo),
            patch("app.main.serpapi.pesquisar_imagem") as pesquisar_imagem,
        ):
            with self.assertRaises(HTTPException) as erro:
                await buscar_serpapi(
                    request,
                    api="google_lens",
                    consentimento="on",
                    foto_id=123,
                    related_content_id="",
                    target_user_id=2,
                    image_file=None,
                )

        self.assertEqual(erro.exception.status_code, 403)
        pesquisar_imagem.assert_not_called()

    async def test_ultima_busca_e_saldo_persistem_apos_consultar_saldo(self):
        resposta = {"images_results": [{"title": "Novo", "link": "https://example.com/novo"}]}
        with TemporaryDirectory() as diretorio:
            with (
                patch.object(db, "CAMINHO_DB", Path(diretorio) / "users.db"),
                patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            ):
                db.iniciar_banco()
                user_id = db.criar_usuario("cliente", "c@example.com", "hash")
                db.atualizar_perfil_busca(user_id, "Ana", "ana")
                request = self.criar_request("/buscar-serpapi")
                request.state.user = {"id": user_id, "username": "cliente", "role": "admin"}
                for patcher in self.patchers_snapshot:
                    patcher.stop()
                with patch("app.main.serpapi.pesquisar", return_value=resposta):
                    await buscar_serpapi(
                        request, api="google_images", consentimento="on",
                        foto_id=0, related_content_id="", target_user_id=0,
                        image_file=None,
                    )
                with patch(
                    "app.main.serpapi.obter_status_conta",
                    return_value={"plan_name": "Free", "searches_per_month": 100, "this_month_usage": 3},
                ):
                    saldo = await status_serpapi(request, target_user_id=0)
                    pagina = pagina_busca_na_web(request)

                for corpo in (saldo.body.decode(), pagina.body.decode()):
                    self.assertIn("foram enviados como candidatos", corpo)
                    self.assertIn("Free", corpo)
                self.assertEqual(db.listar_candidatos(user_id)[0]["title"], "Novo")
                with db.conexao() as conn:
                    total = conn.execute("SELECT COUNT(*) AS n FROM search_snapshots").fetchone()["n"]
                self.assertEqual(total, 2)

    async def test_perfil_e_foto_sao_salvos_e_foto_existente_pode_ser_reusada(self):
        imagem_png = BytesIO()
        Image.new("RGB", (20, 20), "#cc5599").save(imagem_png, format="PNG")
        with TemporaryDirectory() as diretorio:
            caminho_db = Path(diretorio) / "users.db"
            pasta_fotos = Path(diretorio) / "fotos"
            pasta_fotos.mkdir()
            with (
                patch.object(db, "CAMINHO_DB", caminho_db),
                patch("app.main.PASTA_FOTOS_BUSCA", pasta_fotos),
            ):
                db.iniciar_banco()
                user_id = db.criar_usuario("cliente", "cliente@example.com", "hash")
                request = self.criar_request("/perfil-busca")
                request.state.user = {
                    "id": user_id,
                    "username": "cliente",
                    "role": "cliente",
                }
                perfil_response = salvar_perfil_busca(
                    request, "BlackDragon", "@user875s"
                )
                self.assertEqual(perfil_response.status_code, 303)
                self.assertEqual(
                    db.obter_usuario_por_id(user_id)["public_handle"], "user875s"
                )
                pagina_perfil = perfil_busca(request)
                self.assertIn("BlackDragon", pagina_perfil.body.decode())
                self.assertIn("@user875s", pagina_perfil.body.decode())

                with (
                    patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
                    patch(
                        "app.main.serpapi.pesquisar_imagem",
                        return_value={"visual_matches": []},
                    ) as pesquisar_imagem,
                ):
                    primeira_busca = await buscar_serpapi(
                        request,
                        api="google_lens",
                        consentimento="on",
                        foto_id=0,
                        related_content_id="",
                        image_file=UploadFile(
                            filename="foto.png",
                            file=BytesIO(imagem_png.getvalue()),
                        ),
                        target_user_id=user_id,
                    )

                self.assertIn("Resultado da busca: Google Lens API", primeira_busca.body.decode())
                foto = db.listar_fotos_busca(user_id)[0]
                self.assertTrue((pasta_fotos / foto["arquivo"]).is_file())

                outro_id = db.criar_usuario(
                    "outro", "outro@example.com", "hash"
                )
                request.state.user = {
                    "id": outro_id,
                    "username": "outro",
                    "role": "cliente",
                }
                with self.assertRaises(HTTPException) as erro:
                    obter_foto_busca(request, foto["id"])
                self.assertEqual(erro.exception.status_code, 403)

                request.state.user = {
                    "id": user_id,
                    "username": "cliente",
                    "role": "cliente",
                }
                with (
                    patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
                    patch(
                        "app.main.serpapi.pesquisar_imagem",
                        return_value={"visual_matches": []},
                    ) as pesquisar_imagem,
                ):
                    await buscar_serpapi(
                        request,
                        api="google_lens",
                        consentimento="on",
                        foto_id=foto["id"],
                        related_content_id="",
                        image_file=None,
                        target_user_id=user_id,
                    )
                pesquisar_imagem.assert_called_once()
                self.assertEqual(len(db.listar_fotos_busca(user_id)), 1)

    async def test_consulta_de_saldo_nao_dispara_busca_paga(self):
        conta = {
            "plan_name": "Free",
            "searches_per_month": 250,
            "plan_searches_left": 200,
            "total_searches_left": 200,
            "this_month_usage": 50,
        }
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch(
                "app.main.db.obter_usuario_por_id",
                return_value={
                    "id": 1,
                    "username": "cliente",
                    "role": "cliente",
                    "public_name": "BlackDragon",
                    "public_handle": "user875s",
                },
            ),
            patch("app.main.db.listar_fotos_busca", return_value=[]),
            patch("app.main.db.listar_sites_oficiais_usuario", return_value=[]),
            patch("app.main.db.listar_sites_vazados", return_value=[]),
            patch("app.main.serpapi.obter_status_conta", return_value=conta) as status,
            patch("app.main.serpapi.pesquisar") as pesquisar,
        ):
            resultado = await status_serpapi(
                self.criar_request("/status-serpapi"), target_user_id=1
            )

        self.assertEqual(resultado.status_code, 200)
        status.assert_called_once_with()
        pesquisar.assert_not_called()

    async def test_pagina_consulta_saldo_automaticamente_e_preserva_interface(self):
        usuario = {
            "id": 1, "username": "cliente", "role": "cliente",
            "public_name": "Ana", "public_handle": "ana",
        }
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch("app.main.db.obter_usuario_por_id", return_value=usuario),
            patch("app.main.db.listar_fotos_busca", return_value=[]),
            patch("app.main.db.listar_conteudos", return_value=[]),
            patch("app.main.db.listar_sites_vazados", return_value=[]),
            patch("app.main.serpapi.obter_status_conta", return_value={
                "plan_name": "Free", "searches_per_month": 100, "this_month_usage": 3,
            }) as status,
            patch("app.main.serpapi.pesquisar") as pesquisar,
        ):
            request = self.criar_request("/busca-web")
            request.state.user["role"] = "admin"
            corpo = pagina_busca_na_web(request).body.decode()
        status.assert_called_once_with()
        pesquisar.assert_not_called()
        self.assertIn("Atualizar saldo", corpo)
        self.assertIn("Free", corpo)
        for endpoint in ("google_images", "google_images_light", "google_lens",
                         "google_images_related_content"):
            self.assertIn(f'value="{endpoint}"', corpo)
        for texto in ("CAMADA DE PROVEDORES", "Privacidade e consumo de créditos",
                      "Google Cloud Vision", "Resultados validados como vazados",
                      "Termos da busca:", "Chave SerpApi configurada no servidor."):
            self.assertNotIn(texto, corpo)

    async def test_erro_ao_carregar_saldo_e_exibido(self):
        usuario = {
            "id": 1, "username": "cliente", "role": "cliente",
            "public_name": "Ana", "public_handle": "ana",
        }
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch("app.main.db.obter_usuario_por_id", return_value=usuario),
            patch("app.main.db.listar_fotos_busca", return_value=[]),
            patch("app.main.db.listar_conteudos", return_value=[]),
            patch("app.main.db.listar_sites_vazados", return_value=[]),
            patch("app.main.serpapi.obter_status_conta", side_effect=serpapi.ErroSerpApi("erro")),
        ):
            request = self.criar_request("/busca-web")
            request.state.user["role"] = "admin"
            corpo = pagina_busca_na_web(request).body.decode()
        self.assertIn("Não foi possível consultar a conta SerpApi", corpo)

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
            patch("app.main.db.obter_usuario_por_id", return_value={"id": 1, "username": "cliente", "role": "cliente", "public_name": "", "public_handle": ""}),
            patch("app.main.db.listar_fotos_busca", return_value=[]),
            patch("app.main.db.listar_sites_vazados", return_value=[]),
            patch("app.main.db.listar_sites_oficiais_usuario", return_value=[]),
            patch("app.main.db.listar_conteudos", return_value=[]),
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
        self.assertNotIn("Perfil Exemplo", corpo)

    async def test_instagram_popular_envia_apenas_keyword(self):
        with (
            patch.dict(os.environ, {"SERPAPI_API_KEY": "chave-de-teste"}),
            patch("app.main.db.obter_usuario_por_id", return_value={"id": 1, "username": "cliente", "role": "cliente", "public_name": "", "public_handle": ""}),
            patch("app.main.db.listar_fotos_busca", return_value=[]),
            patch("app.main.db.listar_sites_vazados", return_value=[]),
            patch("app.main.db.listar_sites_oficiais_usuario", return_value=[]),
            patch("app.main.db.listar_conteudos", return_value=[]),
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
