import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.requests import Request

from app import db
from app.core import imagens
from app.main import (
    PASTA_OFICIAIS,
    adicionar_candidato_imagem,
    app,
    atualizar_status_candidato_imagem,
)


def imagem_png():
    buffer = BytesIO()
    Image.new("RGB", (20, 20), "#cc5599").save(buffer, format="PNG")
    return buffer.getvalue()


class CandidatoImagemTest(unittest.TestCase):
    def test_persiste_hashes_deduplica_e_isola_candidatos_por_usuario(self):
        with TemporaryDirectory() as diretorio:
            with patch.object(db, "CAMINHO_DB", Path(diretorio) / "users.db"):
                db.iniciar_banco()
                usuario = db.criar_usuario("ana", "ana@example.com", "hash")
                outro = db.criar_usuario("bia", "bia@example.com", "hash")

                candidato_id = db.adicionar_candidato_imagem(
                    usuario,
                    "https://example.com/post",
                    "Resultado",
                    "google_images",
                    "a" * 64,
                    "b" * 16,
                )
                duplicado = db.adicionar_candidato_imagem(
                    usuario,
                    "https://example.com/outro-post",
                    "Outro resultado",
                    "google_images",
                    "a" * 64,
                    "c" * 16,
                )

                self.assertIsNotNone(candidato_id)
                self.assertIsNone(duplicado)
                self.assertEqual(len(db.listar_candidatos_imagem(usuario)), 1)
                self.assertEqual(db.listar_candidatos_imagem(outro), [])
                self.assertFalse(
                    db.atualizar_status_candidato_imagem(
                        candidato_id, outro, "dismissed"
                    )
                )
                self.assertTrue(
                    db.atualizar_status_candidato_imagem(
                        candidato_id, usuario, "dismissed"
                    )
                )
                self.assertEqual(
                    db.listar_candidatos_imagem(usuario)[0]["status"], "dismissed"
                )

    def criar_request(self):
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/candidatos-imagem",
                "headers": [],
                "query_string": b"",
                "server": ("test", 80),
                "client": ("test", 1),
                "scheme": "http",
                "http_version": "1.1",
                "root_path": "",
                "app": app,
                "state": {"user": {"id": 9, "role": "admin"}},
                "session": {"csrf_token": "teste"},
            }
        )

    def test_nao_compara_resultado_de_pagina_oficial_de_outra_pessoa(self):
        with (
            patch(
                "app.main.db.obter_usuario_por_id",
                return_value={"id": 1, "username": "ana"},
            ),
            patch(
                "app.main.db.listar_paginas_oficiais_com_donos",
                return_value=[
                    {"user_id": 2, "url": "https://example.com/perfil"}
                ],
            ),
            patch("app.main.db.adicionar_candidato_imagem") as salvar,
        ):
            with self.assertRaises(HTTPException) as erro:
                asyncio.run(
                    adicionar_candidato_imagem(
                        self.criar_request(),
                        target_user_id=1,
                        source_url="https://example.com/perfil",
                        titulo="Foto",
                        engine="google_images",
                        consentimento="on",
                        arquivo=UploadFile(
                            filename="foto.png", file=BytesIO(imagem_png())
                        ),
                    )
                )

        self.assertEqual(erro.exception.status_code, 400)
        salvar.assert_not_called()

    def test_salva_somente_hashes_para_resultado_sem_origem_oficial(self):
        conteudo = imagem_png()
        with (
            patch(
                "app.main.db.obter_usuario_por_id",
                return_value={"id": 1, "username": "ana"},
            ),
            patch("app.main.db.listar_paginas_oficiais_com_donos", return_value=[]),
            patch("app.main.db.adicionar_candidato_imagem", return_value=7) as salvar,
        ):
            resposta = asyncio.run(
                adicionar_candidato_imagem(
                    self.criar_request(),
                    target_user_id=1,
                    source_url="https://example.com/post",
                    titulo="Foto encontrada",
                    engine="google_images_light",
                    consentimento="on",
                    arquivo=UploadFile(
                        filename="foto.png", file=BytesIO(conteudo)
                    ),
                )
            )

        salvar.assert_called_once_with(
            1,
            "https://example.com/post",
            "Foto encontrada",
            "google_images_light",
            imagens.calcular_sha256(conteudo),
            imagens.calcular_phash(conteudo),
        )
        self.assertEqual(resposta.status_code, 303)

    def test_nao_atualiza_status_de_candidato_de_outro_perfil(self):
        with (
            patch(
                "app.main.db.obter_usuario_por_id",
                return_value={"id": 1, "username": "ana"},
            ),
            patch(
                "app.main.db.atualizar_status_candidato_imagem", return_value=False
            ),
        ):
            with self.assertRaises(HTTPException) as erro:
                atualizar_status_candidato_imagem(
                    self.criar_request(),
                    candidate_id=4,
                    status="dismissed",
                    target_user_id=1,
                )

        self.assertEqual(erro.exception.status_code, 404)

    def test_correspondencia_exata_usa_somente_catalogo_recebido(self):
        conteudo = imagem_png()
        with TemporaryDirectory() as diretorio:
            arquivo = Path(diretorio) / "referencia.png"
            arquivo.write_bytes(conteudo)
            linha = {
                "conteudo_id": 1,
                "arquivo": arquivo.name,
                "criadora": "Ana",
                "phash": imagens.calcular_phash(conteudo),
            }
            with patch("app.main.PASTA_OFICIAIS", Path(diretorio)):
                from app.main import _correspondencias_candidato

                correspondencias = _correspondencias_candidato(
                    {
                        "phash": imagens.calcular_phash(conteudo),
                        "sha256": imagens.calcular_sha256(conteudo),
                    },
                    [linha],
                )

        self.assertEqual(correspondencias[0]["classificacao"], "cópia exata")
        self.assertEqual(correspondencias[0]["distancia"], 0)


if __name__ == "__main__":
    unittest.main()
