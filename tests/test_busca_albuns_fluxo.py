"""Fluxo completo sem rede: M1/M2, pontuacao do album, log e agrupamento."""

import uuid

from app import db
from app.core import busca_provedores, fluxo_busca, pontuacao
from app.core.busca_provedores import Candidato

CONSULTA_M1 = "Morph1ne2.0 OR Morph1neee_ vazado"
CONSULTA_M2 = "morph1ne videos bunkr"
ALBUM_BUNKR = "https://bunkr.cr/a/7W7gu1yU"
ALBUM_INDICE = "https://bunkrsearch.com/albums/7W7gu1yU"


def _candidato(url, titulo="", trecho=""):
    normalizada = pontuacao.normalizar_url(url)
    return Candidato(
        url=normalizada,
        dominio=pontuacao.dominio_de(normalizada),
        titulo=titulo,
        trecho=trecho,
        provedor="tinyfish",
        resposta_bruta={"query": titulo, "resultado": {"url": normalizada}},
    )


def _criadora_teste():
    db.iniciar_banco()
    sufixo = uuid.uuid4().hex[:10]
    user_id = db.criar_usuario(
        f"teste_album_{sufixo}",
        f"teste_album_{sufixo}@example.com",
        "hash",
        public_name="Morph1ne",
        public_handle="Morph1ne2.0",
    )
    return {"id": user_id, "public_name": "Morph1ne", "public_handle": "Morph1ne2.0"}


def test_fluxo_acha_album_com_modelos_e_registra_log(monkeypatch):
    criadora = _criadora_teste()

    def fake_buscar(provedor, consulta, **opcoes):
        if consulta == CONSULTA_M1:
            return [
                _candidato(f"https://exemplo.example/pagina-{indice}", "Pagina sem relacao")
                for indice in range(5)
            ] + [_candidato(ALBUM_BUNKR, "morph1ne - Videos", "album de videos")]
        if consulta == CONSULTA_M2:
            return [
                _candidato(ALBUM_BUNKR, "morph1ne - Videos", "album de videos"),
                _candidato(ALBUM_INDICE, "morph1ne - Videos", ""),
                _candidato("https://bunkr.net/video/123", "Bunkr - Video hosting", ""),
            ]
        return []

    monkeypatch.setattr(fluxo_busca, "_passo_instagram", lambda alvo: ("", [
        "Morph1ne2.0",
        "Morph1neee_",
    ], False))
    monkeypatch.setattr(busca_provedores, "buscar", fake_buscar)

    resumo = fluxo_busca.executar(
        criadora,
        "tinyfish",
        termos=["vazado"],
        sites=[],
        limite=18,
    )

    assert resumo["nome_base"] == "morph1ne"
    assert CONSULTA_M1 in resumo["consultas"]
    assert CONSULTA_M2 in resumo["consultas"]
    assert resumo["consultas"][0] == CONSULTA_M1

    log = db.listar_consulta_log(resumo["execucao_id"])
    por_consulta = {item["consulta_exata"]: item for item in log}
    assert por_consulta[CONSULTA_M1]["modelo"] == "M1"
    assert por_consulta[CONSULTA_M2]["modelo"] == "M2"
    # O album estava na posicao 6 da M1.
    assert por_consulta[CONSULTA_M1]["posicao_do_candidato"] == 6
    # O album do bunkr ja veio da M1; a M2 contribuiu com o indice na posicao 2.
    assert por_consulta[CONSULTA_M2]["posicao_do_candidato"] == 2

    candidatos = {
        linha["url"]: linha
        for linha in db.listar_candidatos_revisao(criadora["id"], "pending", 0)
    }
    album = candidatos[ALBUM_BUNKR]
    assert album["chave_grupo"] == "album:7W7gu1yU"
    assert album["consulta_modelo"] == "M1"
    assert album["posicao_busca"] == 6
    assert album["nota"] >= 45

    indice = candidatos[ALBUM_INDICE]
    assert indice["chave_grupo"] == "album:7W7gu1yU"
    assert indice["consulta_modelo"] == "M2"

    # O bunkr.net (empresa de video) nao entra na revisao.
    assert "https://bunkr.net/video/123" not in candidatos
    assert resumo["grupos"] == 1
