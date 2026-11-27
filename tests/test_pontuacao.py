import unittest

from app.core import pontuacao


class NormalizacaoTest(unittest.TestCase):
    def test_remove_rastreio_maiusculas_e_barras_finais(self):
        a = pontuacao.normalizar_url(
            "https://WWW.Exemplo.com/post/1/?utm_source=x&b=2&a=1#topo"
        )
        b = pontuacao.normalizar_url("http://exemplo.com/post/1?a=1&b=2")
        self.assertEqual(a, b)
        self.assertEqual(a, "https://exemplo.com/post/1?a=1&b=2")

    def test_descarta_url_sem_http_ou_dominio(self):
        self.assertEqual(pontuacao.normalizar_url("javascript:alert(1)"), "")
        self.assertEqual(pontuacao.normalizar_url(""), "")


class AvaliacaoTest(unittest.TestCase):
    def test_perfil_oficial_da_criadora(self):
        resultado = pontuacao.avaliar(
            "https://www.instagram.com/ana/",
            "Ana (@ana) • Instagram",
            "",
            arrobas=["ana"],
            dominios_vazamento=["erome.com"],
        )
        self.assertEqual(resultado["classificacao"], "oficial")
        self.assertEqual(resultado["nota"], 0)

    def test_album_de_hospedagem_com_arroba_no_titulo(self):
        resultado = pontuacao.avaliar(
            "https://www.erome.com/a/xyz?utm_source=telegram",
            "Ana @ana vazado",
            "download do pack completo",
            arrobas=["ana"],
            dominios_vazamento=["erome.com"],
        )
        self.assertEqual(resultado["classificacao"], "provavel_vazamento")
        self.assertGreaterEqual(resultado["nota"], 60)
        self.assertTrue(any("@ana" in motivo for motivo in resultado["motivos"]))
        self.assertTrue(any("erome.com" in motivo for motivo in resultado["motivos"]))

    def test_pagina_de_busca_generica(self):
        resultado = pontuacao.avaliar(
            "https://www.google.com/search?q=ana+vazado",
            "ana vazado - Google",
            "",
            arrobas=["ana"],
            dominios_vazamento=["erome.com"],
        )
        self.assertEqual(resultado["classificacao"], "falso_positivo")
        self.assertEqual(resultado["nota"], 0)

    def test_espelho_de_perfil(self):
        resultado = pontuacao.avaliar(
            "https://espelho.example/ana",
            "Perfil de @ana",
            "",
            arrobas=["ana"],
            dominios_vazamento=["erome.com"],
        )
        self.assertEqual(resultado["classificacao"], "falso_positivo")
        self.assertIn("espelho", resultado["motivos"][0])


if __name__ == "__main__":
    unittest.main()
