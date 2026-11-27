from app import db
from app.core import instagram, pontuacao


def test_perfil_em_plataforma_diferente_e_oficial_sem_cadastro():
    for url in (
        "https://www.privacy.com.br/ana",
        "https://onlyfans.com/ana",
        "https://x.com/ana",
        "https://linktr.ee/ana",
    ):
        resultado = pontuacao.avaliar(url, "Ana", "", arrobas=["ana"])
        assert resultado["classificacao"] == "oficial", url
        assert resultado["nota"] == 0


def test_pagina_em_dominio_aprovado_na_revisao_ja_pontua():
    # A lista deixou de ser fixa no codigo: so dominio aprovado na revisao soma.
    db.iniciar_banco()
    db.semear_dominios(
        [{"dominio": "fapello.com", "tipo": "agregador_vazamento", "status": "aprovado"}]
    )
    resultado = pontuacao.avaliar(
        "https://fapello.com/ana/",
        "Ana",
        "",
        arrobas=["ana"],
    )
    assert resultado["classificacao"] == "provavel_vazamento"
    assert resultado["nota"] >= 60
    assert any("fapello.com" in motivo for motivo in resultado["motivos"])


def test_dominio_configurado_soma_aos_conhecidos():
    resultado = pontuacao.avaliar(
        "https://site-novo.example/post/ana",
        "Ana vazado",
        "",
        arrobas=["ana"],
        dominios_vazamento=["site-novo.example"],
    )
    assert resultado["classificacao"] == "provavel_vazamento"


def test_nome_publico_conta_como_sinal():
    com_nome = pontuacao.avaliar(
        "https://exemplo.example/post/123",
        "Ana Silva vazada",
        "",
        arrobas=["ana"],
        nome="Ana Silva",
    )
    sem_nome = pontuacao.avaliar(
        "https://exemplo.example/post/123",
        "Ana Silva vazada",
        "",
        arrobas=["ana"],
    )
    assert com_nome["nota"] > sem_nome["nota"]
    assert any("nome público" in motivo for motivo in com_nome["motivos"])


def test_link_da_bio_marca_pagina_como_oficial():
    resultado = pontuacao.avaliar(
        "https://anasilva.com.br/links",
        "Ana Silva",
        "",
        arrobas=["ana"],
        paginas_oficiais=["https://anasilva.com.br"],
    )
    assert resultado["classificacao"] == "oficial"


def test_extrair_links_da_bio_ignora_email_e_dominios_de_vazamento():
    bio = (
        "contato: ana@gmail.com\n"
        "site: anasilva.com.br\n"
        "https://linktr.ee/ana?utm_source=ig\n"
        "meu vazado: https://fapello.com/ana/"
    )
    links = instagram.extrair_links(bio)
    assert "https://anasilva.com.br/" in links
    assert "https://linktr.ee/ana" in links
    assert all("gmail.com" not in link for link in links)
    assert all("fapello.com" not in link for link in links)
