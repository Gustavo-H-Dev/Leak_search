from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from starlette.requests import Request

from app import db
from app.core import candidatos, imagens
from app.main import (
    _enviar_para_valida,
    app,
    valida,
    validar_candidato,
)


def criar_request(user_id, role="cliente"):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/valida",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 1),
            "scheme": "http",
            "http_version": "1.1",
            "root_path": "",
            "app": app,
            "state": {"user": {"id": user_id, "username": "ana", "role": role}},
        }
    )


class ValidaTest(unittest.TestCase):
    def setUp(self):
        diretorio = TemporaryDirectory()
        self.addCleanup(diretorio.cleanup)
        patcher = patch.object(db, "CAMINHO_DB", Path(diretorio.name) / "users.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.iniciar_banco()
        self.user_id = db.criar_usuario("ana", "ana@example.com", "hash")

    def test_envio_omite_oficiais_filhas_nao_sou_eu_e_repetidos(self):
        user_id = self.user_id
        db.adicionar_site_oficial_usuario(
            user_id, user_id, "Perfil", "https://example.com/perfil"
        )
        db.adicionar_candidato(
            user_id, "https://example.com/recusado", "x", "", "", "", "serp", ""
        )
        candidato = db.listar_candidatos(user_id)[0]
        db.atualizar_status_candidato(candidato["id"], user_id, "not_me")
        resultados = [
            {"link": "https://example.com/perfil/post/1", "titulo": "Filha"},
            {"link": "https://example.com/recusado", "titulo": "Recusado"},
            {"link": "https://outro.com/a", "titulo": "Novo", "miniatura": "https://cdn.com/a.jpg"},
            {"link": "https://outro.com/a/", "titulo": "Repetido"},
        ]

        with patch(
            "app.main.candidatos_core.calcular_hashes",
            return_value={"https://cdn.com/a.jpg": "a" * 16},
        ):
            resumo = _enviar_para_valida({"id": user_id}, resultados, "google_images")

        self.assertEqual(
            (resumo["novos"], resumo["oficiais"], resumo["nao_sou_eu"], resumo["repetidos"]),
            (1, 1, 1, 1),
        )
        pendentes = db.listar_candidatos(user_id, "pending")
        self.assertEqual([c["url"] for c in pendentes], ["https://outro.com/a"])
        self.assertEqual(pendentes[0]["phash"], "a" * 16)

    def test_busca_google_images_guarda_opcoes_relacionadas(self):
        from app import main

        resposta = {"images_results": [
            {"link": "https://x.com/1", "title": "Primeiro", "related_content_id": "abc"},
            {"link": "https://x.com/2", "title": "Segundo", "related_content_id": "abc"},
        ]}
        with patch("app.main.serpapi.pesquisar", return_value=resposta):
            main._executar_busca_vazamentos(
                {"id": self.user_id}, "google_images", ["q"], {}
            )
        self.assertEqual(
            main._status_vazamento(self.user_id)["related_options"],
            [{"id": "abc", "title": "Primeiro"}],
        )

    def test_posts_oficiais_do_instagram_importados_nao_viram_candidatos(self):
        user_id = self.user_id
        db.adicionar_hash_oficial(user_id, "https://www.instagram.com/p/AbC", "")
        resumo = _enviar_para_valida(
            {"id": user_id},
            [
                {"link": "https://www.instagram.com/reel/AbC/", "titulo": "Meu post"},
                {"link": "https://www.instagram.com/p/Outro/", "titulo": "Outro"},
            ],
            "google_images",
        )
        self.assertEqual((resumo["oficiais"], resumo["novos"]), (1, 1))

    def test_importacao_instagram_percorre_todas_as_paginas(self):
        import asyncio
        from app.main import importar_posts_oficiais_instagram

        def pagina(codigo, proximo):
            dados = {
                "profile_results": {
                    "username": "ana",
                    "posts": [{"shortcode": codigo, "media_captions": ["x"]}],
                }
            }
            if proximo:
                dados["serpapi_pagination"] = {"next_page_token": proximo}
            return dados

        chamadas = []

        def falsa(engine, **parametros):
            chamadas.append(parametros)
            return pagina("A1", "t2") if "next_page_token" not in parametros else pagina("B2", "")

        request = criar_request(self.user_id, role="admin")
        with patch("app.main.serpapi.pesquisar", side_effect=falsa), patch(
            "app.main._contexto_instagram", side_effect=lambda r, **v: v
        ):
            resposta = asyncio.run(
                importar_posts_oficiais_instagram(
                    request, "on", self.user_id, "ana", 30
                )
            )
        self.assertEqual(len(chamadas), 2)
        self.assertEqual(chamadas[1]["next_page_token"], "t2")
        self.assertEqual(
            sorted(h["url"] for h in db.listar_hashes_oficiais(self.user_id)),
            ["https://www.instagram.com/p/A1", "https://www.instagram.com/p/B2"],
        )
        self.assertIn("2 publicações", resposta["mensagem"])

    def test_pagina_ordena_atencao_possiveis_e_diferentes(self):
        user_id = self.user_id
        referencia = imagens.calcular_phash(_png("#cc5599"))
        db.adicionar_hash_oficial(user_id, "https://example.com/perfil", referencia)
        longe = format(int(referencia, 16) ^ ((1 << 40) - 1), "016x")
        medio = format(int(referencia, 16) ^ ((1 << 14) - 1), "016x")
        for url, phash in (
            ("https://a.com/longe", longe),
            ("https://a.com/medio", medio),
            ("https://a.com/igual", referencia),
        ):
            db.adicionar_candidato(user_id, url, url, "", "", "", "serp", phash)

        corpo = valida(criar_request(user_id)).body.decode()

        self.assertLess(corpo.index("a.com/igual"), corpo.index("a.com/medio"))
        self.assertLess(corpo.index("a.com/medio"), corpo.index("a.com/longe"))
        self.assertIn("Atenção", corpo)

    def test_validacao_vazado_e_oficial_atualizam_registros_do_perfil(self):
        user_id = self.user_id
        db.adicionar_candidato(user_id, "https://a.com/v", "Vazado", "", "", "", "serp", "")
        db.adicionar_candidato(user_id, "https://a.com/o", "Oficial", "", "", "", "serp", "b" * 16)
        ids = {c["url"]: c["id"] for c in db.listar_candidatos(user_id)}

        validar_candidato(criar_request(user_id), ids["https://a.com/v"], "leaked", 0)
        validar_candidato(criar_request(user_id), ids["https://a.com/o"], "official", 0)

        self.assertEqual(db.listar_sites_vazados(user_id)[0]["url"], "https://a.com/v")
        self.assertEqual(db.listar_sites_oficiais_usuario(user_id)[0]["url"], "https://a.com/o")
        self.assertEqual(db.listar_hashes_oficiais(user_id)[0]["phash"], "b" * 16)
        self.assertEqual(db.listar_candidatos(user_id, "pending"), [])

    def test_download_recusa_destino_nao_publico(self):
        with patch("app.core.candidatos.httpx.Client") as cliente:
            self.assertEqual(
                candidatos.hash_da_imagem_remota("http://127.0.0.1/a.jpg"), ""
            )
        cliente.return_value.__enter__.return_value.stream.assert_not_called()


def _png(cor):
    from io import BytesIO
    from PIL import Image, ImageDraw

    imagem = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(imagem).ellipse((8, 8, 40, 56), fill=cor)
    buffer = BytesIO()
    imagem.save(buffer, format="PNG")
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()


def test_analise_aponta_vazados_em_diferentes():
    from app.core import analise

    referencias = [{"phash": "0" * 16, "origem": "x"}]
    lista = [
        {"url": f"https://a.com/{i}", "engine": "g", "status": "leaked", "phash": "f" * 16}
        for i in range(3)
    ] + [{"url": "https://a.com/n", "engine": "g", "status": "pending", "phash": ""}]
    relatorio = analise.analisar_usuario(lista, referencias, 0)
    assert relatorio["niveis"]["leaked"] == {"diferente": 3}
    assert relatorio["sem_hash"] == 1
    assert any("Muitos vazados" in a for a in relatorio["alertas"])


def test_busca_de_vazamentos_em_segundo_plano_registra_status(tmp_path):
    from app import main

    with patch.object(db, "CAMINHO_DB", tmp_path / "u.db"):
        db.iniciar_banco()
        uid = db.criar_usuario("ana", "ana@example.com", "hash")
        falsa = {"images_results": [{"link": "https://x.com/a", "original": "https://x.com/a.jpg", "title": "t"}]}
        with patch("app.main.serpapi.pesquisar", return_value=falsa), patch(
            "app.main.candidatos_core.calcular_hashes", return_value={}
        ):
            main._executar_busca_vazamentos({"id": uid}, "google_images_light", ["q1", "q2"], {})
        status = main._status_vazamento(uid)
        assert status["estado"] == "ok" and status["resumo"]["novos"] == 1

        with patch("app.main.serpapi.pesquisar", side_effect=RuntimeError("falhou")):
            main._executar_busca_vazamentos({"id": uid}, "google_images_light", ["q1"], {})
        assert main._status_vazamento(uid)["estado"] == "erro"


def test_busca_de_vazamentos_ignora_consulta_sem_resultados(tmp_path):
    from app import main
    from app.core import serpapi

    falsa = {"images_results": [{"link": "https://x.com/a", "original": "https://x.com/a.jpg", "title": "t"}]}

    def pesquisar(engine, q, **_):
        if q == "vazia":
            raise serpapi.SemResultados("Google Images Light hasn't returned any results for this query.")
        return falsa

    with patch.object(db, "CAMINHO_DB", tmp_path / "u.db"):
        db.iniciar_banco()
        uid = db.criar_usuario("ana", "ana@example.com", "hash")
        with patch("app.main.serpapi.pesquisar", side_effect=pesquisar), patch(
            "app.main.candidatos_core.calcular_hashes", return_value={}
        ):
            main._executar_busca_vazamentos({"id": uid}, "google_images_light", ["vazia", "ok"], {})
        status = main._status_vazamento(uid)
        assert status["estado"] == "ok"
        assert [i["resultados"] for i in status["por_consulta"]] == [0, 1]


def test_admin_ve_todas_as_criadoras_e_pesquisa_pelo_usuario(tmp_path):
    from app import main

    with patch.object(db, "CAMINHO_DB", tmp_path / "u.db"):
        db.iniciar_banco()
        admin = db.criar_usuario("adm", "adm@example.com", "h")
        criadora = db.criar_usuario("bia", "bia@example.com", "h")
        assert {u["username"] for u in db.listar_usuarios_para_busca()} == {"adm", "bia"}
        alvo = main._usuario_alvo_busca(criar_request(admin, role="admin"), criadora)
        assert (alvo["public_name"], alvo["public_handle"]) == ("bia", "bia")
