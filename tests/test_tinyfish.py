import os
import unittest
from unittest.mock import patch

import httpx

from app.core import tinyfish


def _resposta(status, payload=None, headers=None):
    return httpx.Response(
        status,
        json=payload if payload is not None else {},
        headers=headers or {},
        request=httpx.Request("GET", tinyfish.API_URL),
    )


class TinyFishTest(unittest.TestCase):
    def test_sem_chave_nao_faz_requisicao(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("app.core.tinyfish.httpx.get") as get,
        ):
            with self.assertRaises(tinyfish.TinyFishNaoConfigurada):
                tinyfish.pesquisar("ana vazado")
        get.assert_not_called()

    def test_repetir_em_429_e_devolver_resultados(self):
        respostas = [
            _resposta(429, {"code": "RATE_LIMIT_EXCEEDED"}),
            _resposta(200, {"query": "ana", "results": [], "total_results": 0}),
        ]
        with (
            patch.dict(os.environ, {"TINYFISH_API_KEY": "segredo"}),
            patch("app.core.tinyfish.httpx.get", side_effect=respostas) as get,
            patch("app.core.tinyfish.time.sleep") as dormir,
        ):
            dados = tinyfish.pesquisar("ana vazado")

        self.assertEqual(dados["results"], [])
        self.assertEqual(get.call_count, 2)
        self.assertTrue(dormir.called)
        self.assertEqual(
            get.call_args.kwargs["headers"], {"X-API-Key": "segredo"}
        )
        self.assertEqual(
            get.call_args.kwargs["params"],
            {"query": "ana vazado", "location": "BR", "language": "pt"},
        )

    def test_402_explica_falta_de_creditos(self):
        with (
            patch.dict(os.environ, {"TINYFISH_API_KEY": "segredo"}),
            patch(
                "app.core.tinyfish.httpx.get",
                return_value=_resposta(402, {"code": "INSUFFICIENT_CREDITS"}),
            ),
        ):
            with self.assertRaises(tinyfish.TinyFishSemCreditos) as erro:
                tinyfish.pesquisar("ana vazado")
        self.assertIn("sem saldo", str(erro.exception))


if __name__ == "__main__":
    unittest.main()
