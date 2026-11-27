"""Dominios suspeitos: sinais, sugestao (nunca aprovacao) e revisao humana."""

import uuid

from app import db
from app.core import dominios_suspeitos, pontuacao


def _criadora() -> int:
    db.iniciar_banco()
    sufixo = uuid.uuid4().hex[:10]
    return db.criar_usuario(
        f"dom_{sufixo}",
        f"dom_{sufixo}@example.com",
        "hash",
        public_name="Morph1ne",
        public_handle="Morph1ne2.0",
    )


def _execucao(criadora_id: int, urls):
    execucao_id = db.criar_busca_execucao(criadora_id, "teste", ["vazado"])
    db.salvar_candidatos_busca(
        criadora_id,
        execucao_id,
        [
            {
                "url_normalizada": pontuacao.normalizar_url(url),
                "dominio": pontuacao.dominio_de(pontuacao.normalizar_url(url)),
                "titulo": titulo,
                "trecho": trecho,
                "provedor": "teste",
                "nota": 80,
                "motivos": [],
                "classificacao": "provavel_vazamento",
                "chave_grupo": "",
                "consulta_log_id": None,
                "posicao_busca": 1,
            }
            for url, titulo, trecho in urls
        ],
    )
    return execucao_id


def _avaliar(execucao_id):
    return dominios_suspeitos.avaliar_dominios(
        execucao_id,
        arrobas=["Morph1ne2.0"],
        variantes=("morph1ne",),
        nome_base="morph1ne",
    )


def _dominio_unico(nome="site") -> str:
    return f"{nome}-{uuid.uuid4().hex[:8]}.example"


def test_dominio_ignorado_nunca_e_sugerido():
    criadora_id = _criadora()
    url = "https://tiktok.com/@morph1ne/video/123"
    for _ in range(2):
        execucao = _execucao(
            criadora_id,
            [(url, "morph1ne - Videos vazado", "full videos /a/7W7gu1yU files 20 MB")],
        )
        assert _avaliar(execucao) == []

    assert db.obter_dominio_suspeito("tiktok.com") is None
    assert db.contagens_do_dominio("tiktok.com")["execucoes"] == 0


def test_uma_aparicao_em_uma_execucao_nao_e_sugerido():
    criadora_id = _criadora()
    dominio = _dominio_unico("uma-execucao")
    execucao = _execucao(
        criadora_id,
        [
            (
                f"https://{dominio}/a/7W7gu1yU",
                "morph1ne - Videos vazado",
                "full videos",
            )
        ],
    )

    assert _avaliar(execucao) == []
    assert db.obter_dominio_suspeito(dominio) is None
    # A execucao fica registrada para a contagem da proxima vez.
    assert db.contagens_do_dominio(dominio)["execucoes"] == 1


def test_sinais_fortes_em_duas_execucoes_viram_sugerido_nunca_aprovado():
    criadora_id = _criadora()
    dominio = _dominio_unico("duas-execucoes")
    for _ in range(2):
        execucao = _execucao(
            criadora_id,
            [
                (
                    f"https://{dominio}/a/7W7gu1yU",
                    "morph1ne - Videos vazado",
                    "full videos, free download",
                )
            ],
        )
        sugestoes = _avaliar(execucao)

    assert [item["dominio"] for item in sugestoes] == [dominio]
    registro = db.obter_dominio_suspeito(dominio)
    assert registro["status"] == "sugerido"
    assert registro["status"] != "aprovado"
    assert registro["tipo"] == "outro"
    assert registro["origem"] == "auto"
    assert registro["n_execucoes_distintas"] == 2
    assert registro["n_aparicoes"] == 2
    assert registro["nota"] >= dominios_suspeitos.limiar()
    assert registro["sinais"]


def test_aprovar_dominio_faz_a_pontuacao_incluir():
    _criadora()
    dominio = _dominio_unico("aprovado")
    url = f"https://{dominio}/a/xyz"

    antes = pontuacao.avaliar(url, "ana vazado", "", arrobas=["ana"])
    assert not any("vazamento" in motivo for motivo in antes["motivos"])

    db.semear_dominios([{"dominio": dominio, "tipo": "agregador_vazamento", "status": "sugerido"}])
    assert db.atualizar_status_dominio(dominio, "aprovado", tipo="agregador_vazamento")

    depois = pontuacao.avaliar(url, "ana vazado", "", arrobas=["ana"])
    assert depois["nota"] == antes["nota"] + 40
    assert any(dominio in motivo for motivo in depois["motivos"])


def test_bunkr_net_fica_no_maximo_sugerido():
    criadora_id = _criadora()
    for _ in range(2):
        execucao = _execucao(
            criadora_id,
            [
                (
                    "https://bunkr.net/video/123",
                    "morph1ne vazado full videos",
                    "free download files",
                )
            ],
        )
        _avaliar(execucao)

    registro = db.obter_dominio_suspeito("bunkr.net")
    assert registro is not None
    assert registro["status"] == "sugerido"
    assert registro["status"] != "aprovado"

    # Sem aprovacao, o dominio nao soma o sinal de vazamento na pontuacao.
    avaliacao = pontuacao.avaliar(
        "https://bunkr.net/video/123", "morph1ne vazado", ""
    )
    assert not any("vazamento" in motivo for motivo in avaliacao["motivos"])


def test_feedback_marca_dominio_aprovado_para_revisar():
    dominio = _dominio_unico("feedback")
    db.semear_dominios(
        [{"dominio": dominio, "tipo": "agregador_vazamento", "status": "aprovado"}]
    )

    # 1 confirmado e 2 falsos: falsos dominam apos o limite de 2 revisoes.
    db.registrar_feedback_dominio(dominio, True, limite=2)
    db.registrar_feedback_dominio(dominio, False, limite=2)
    assert db.obter_dominio_suspeito(dominio)["observacao"] == ""

    db.registrar_feedback_dominio(dominio, False, limite=2)
    registro = db.obter_dominio_suspeito(dominio)
    assert registro["n_confirmados"] == 1
    assert registro["n_falsos"] == 2
    assert registro["observacao"] == "revisar"
    # Nada e removido automaticamente: o dominio continua aprovado.
    assert registro["status"] == "aprovado"


def test_dominio_sem_sinais_nao_e_sugerido():
    criadora_id = _criadora()
    dominio = _dominio_unico("sem-sinais")
    for _ in range(2):
        execucao = _execucao(
            criadora_id,
            [(f"https://{dominio}/post/1", "Noticias de tecnologia", "review do produto")],
        )
        _avaliar(execucao)

    assert db.obter_dominio_suspeito(dominio) is None
