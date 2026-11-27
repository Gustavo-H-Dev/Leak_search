from app.core import pontuacao, variantes_nome

ARROBAS = ["morph1ne2.0", "Morph1neee_"]
VARIANTES = variantes_nome.variantes_dos_arrobas(ARROBAS)


def test_album_bunkr_com_nome_base_no_titulo():
    resultado = pontuacao.avaliar(
        "https://bunkr.cr/a/7W7gu1yU",
        "morph1ne - Videos",
        "album de videos",
        arrobas=ARROBAS,
        variantes=VARIANTES,
        nome_base="morph1ne",
    )
    assert resultado["classificacao"] != "falso_positivo"
    assert resultado["nota"] >= 45
    assert any("sinal mais forte" in motivo for motivo in resultado["motivos"])
    assert any("bunkr.cr" in motivo for motivo in resultado["motivos"])


def test_indice_de_albuns_tambem_e_sinal_forte():
    resultado = pontuacao.avaliar(
        "https://bunkrsearch.com/albums/7W7gu1yU",
        "morph1ne - Videos",
        "",
        arrobas=ARROBAS,
        variantes=VARIANTES,
        nome_base="morph1ne",
    )
    assert resultado["nota"] >= 45
    assert any("indice_albuns" in motivo for motivo in resultado["motivos"])


def test_variante_em_site_comum_pontua_menos_que_o_album():
    comum = pontuacao.avaliar(
        "https://exemplo.example/post/1",
        "morph1ne videos",
        "",
        arrobas=ARROBAS,
        variantes=VARIANTES,
        nome_base="morph1ne",
    )
    assert comum["nota"] == 25
    assert any("variante" in motivo for motivo in comum["motivos"])


def test_site_sem_relacao_nao_pontua():
    resultado = pontuacao.avaliar(
        "https://bunkr.net/video/123",
        "Bunkr - Video hosting",
        "",
        arrobas=ARROBAS,
        variantes=VARIANTES,
        nome_base="morph1ne",
    )
    assert resultado["classificacao"] == "falso_positivo"
    assert resultado["nota"] == 0


def test_chave_grupo_une_o_mesmo_album_entre_sites():
    assert pontuacao.chave_grupo("https://bunkr.cr/a/7W7gu1yU") == "album:7W7gu1yU"
    assert (
        pontuacao.chave_grupo("https://bunkrsearch.com/albums/7W7gu1yU?x=1")
        == "album:7W7gu1yU"
    )
    assert pontuacao.chave_grupo("https://bunkr.cr/a/outro") != "album:7W7gu1yU"
    assert pontuacao.chave_grupo("https://erome.com/a/xyz") == ""
