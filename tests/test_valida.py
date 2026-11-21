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
