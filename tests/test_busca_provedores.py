import unittest
from unittest.mock import patch

from app.core import busca_provedores


class TinyFishAdapterTest(unittest.TestCase):
    def test_converte_results_em_candidatos(self):
        dados = {
            "query": "ana vazado",
            "results": [
                {
                    "position": 1,
                    "site_name": "erome.com",
                    "title": "Ana @ana vazado",
                    "snippet": "download",
                    "url": "https://www.erome.com/a/xyz?utm_source=x",
                },
                {"position": 2, "title": "Inválido", "url": "javascript:alert(1)"},
            ],
            "total_results": 2,
        }
        with patch("app.core.busca_provedores.tinyfish.pesquisar", return_value=dados):
            candidatos = busca_provedores.buscar_tinyfish("ana vazado")

        self.assertEqual(len(candidatos), 1)
        candidato = candidatos[0]
        self.assertEqual(candidato.url, "https://erome.com/a/xyz")
        self.assertEqual(candidato.dominio, "erome.com")
        self.assertEqual(candidato.titulo, "Ana @ana vazado")
        self.assertEqual(candidato.trecho, "download")
        self.assertEqual(candidato.miniatura_url, "")
        self.assertEqual(candidato.provedor, "tinyfish")
        self.assertEqual(candidato.resposta_bruta["query"], "ana vazado")


if __name__ == "__main__":
    unittest.main()
