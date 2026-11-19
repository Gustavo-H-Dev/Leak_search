import os
import unittest
from unittest.mock import patch

from app.core import provedores_busca


class ProvedoresBuscaTest(unittest.TestCase):
    def test_lista_provedores_integrados_sem_chave(self):
        with patch.dict(os.environ, {}, clear=True):
            provedores = provedores_busca.listar_provedores()

        by_id = {provedor["id"]: provedor for provedor in provedores}
        self.assertTrue(by_id["serpapi"]["integrado"])
        self.assertFalse(by_id["serpapi"]["configurado"])
        self.assertEqual(by_id["serpapi"]["status"], "Integrado, não configurado")

    def test_identifica_provedor_serpapi_configurado(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "teste"}, clear=True):
            provedor = provedores_busca.obter_provedor("serpapi")

        self.assertEqual(provedor["status"], "Ativo")
        self.assertTrue(provedor["configurado"])

    def test_provedor_nao_integrado_nao_fica_configurado(self):
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "teste"}, clear=True):
            provedor = provedores_busca.obter_provedor("brave")

        self.assertFalse(provedor["integrado"])
        self.assertFalse(provedor["configurado"])
        self.assertEqual(provedor["status"], "Disponível para integração")

    def test_filtro_por_tipo(self):
        with patch.dict(os.environ, {}, clear=True):
            provedores = provedores_busca.listar_provedores("imagem")

        ids = {provedor["id"] for provedor in provedores}
        self.assertIn("google_vision", ids)
        self.assertIn("serpapi", ids)
        self.assertIn("tineye", ids)
