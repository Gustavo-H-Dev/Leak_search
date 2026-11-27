"""Escaneamento agendado (fase 3): agenda, fila, limites, estados e histórico.

O aceite da fase está em tres testes: duas criadoras juntas respeitam 30
buscas/minuto (janela compartilhada), reiniciar nao duplica execucoes (reserva
atomica) e o historico mostra data, provedor, buscas, novos, duracao e erro.
"""

import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db, worker
from app.core import agendamento, autenticacao, fila_buscas, limite_taxa
from app.main import app

SENHA = "senha-teste-agendamento"


def _csrf(html: str) -> str:
    achado = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert achado, "token CSRF nao encontrado na pagina"
    return achado.group(1)


def _entrar(cliente: TestClient, usuario: str) -> None:
    token = _csrf(cliente.get("/login").text)
    resposta = cliente.post(
        "/login",
        data={"csrf_token": token, "username": usuario, "senha": SENHA, "next": "/"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303, resposta.text


@pytest.fixture()
def criadora(tmp_path):
    with (
        patch.object(db, "CAMINHO_DB", tmp_path / "agendamento.db"),
        patch.dict(
            os.environ,
            {
                "SERPAPI_API_KEY": "",
                "TINYFISH_API_KEY": "",
                "REDIS_URL": "",
                "BUSCAS_POR_DIA_LIMITE": "2",
            },
        ),
    ):
        limite_taxa.configurar(None)
        db.iniciar_banco()
        user_id = db.criar_usuario(
            "criadora_agenda",
            "criadora_agenda@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Mari",
            public_handle="marianax",
        )
        db.confirmar_email(user_id)
        db.adicionar_perfil_monitorado(user_id, "instagram", "marianax")
        with TestClient(app) as cliente:
            _entrar(cliente, "criadora_agenda")
            yield SimpleNamespace(cliente=cliente, id=user_id)
        limite_taxa.configurar(None)


# ------------------------------------------------------------------ a agenda


def test_padrao_e_semanal_na_proxima_segunda_as_nove():
    quarta = datetime(2026, 11, 25, 10, 30, tzinfo=agendamento.FUSO)
    proxima = agendamento.proxima_execucao("semanal", 9, 0, base=quarta)
    assert proxima.weekday() == 0
    assert (proxima.day, proxima.hour, proxima.minute) == (30, 9, 0)
    assert proxima > quarta


def test_diario_pula_para_amanha_quando_a_hora_ja_passou():
    hoje = datetime(2026, 11, 25, 10, 30, tzinfo=agendamento.FUSO)
    proxima = agendamento.proxima_execucao("diario", 9, 0, base=hoje)
    assert (proxima.day, proxima.hour) == (26, 9)

    ainda_hoje = datetime(2026, 11, 25, 7, 0, tzinfo=agendamento.FUSO)
    assert agendamento.proxima_execucao("diario", 9, 0, base=ainda_hoje).day == 25


def test_agendamento_invalido_nao_e_salvo(criadora):
    with pytest.raises(ValueError):
        agendamento.salvar(criadora.id, "cada_hora", 9, 0)
    with pytest.raises(ValueError):
        agendamento.salvar(criadora.id, "semanal", 25, 0)
    assert db.obter_agendamento_busca(criadora.id) is None


def test_salvar_agendamento_persiste_e_descreve(criadora):
    salvo = agendamento.salvar(criadora.id, "diario", 7, 0)
    assert salvo["frequencia"] == "diario"
    assert salvo["resumo"] == "Todos os dias às 07:00"
    registro = db.obter_agendamento_busca(criadora.id)
    assert registro["ativo"] == 1
    assert registro["proxima_em"] > agendamento.em_utc(agendamento.agora())


# --------------------------------------------------------------- limite diario


def test_limite_diario_bloqueia_o_botao_e_o_agendamento(criadora):
    for _ in range(2):
        db.criar_busca_execucao(
            criadora.id, "tinyfish", [], status="concluida"
        )
    execucao_id, mensagem = agendamento.disparar_agora(criadora.id)
    assert execucao_id is None
    assert "Limite de 2 busca" in mensagem
    assert len(db.listar_execucoes_criadora(criadora.id)) == 2


def test_uma_busca_em_andamento_impede_outra(criadora):
    db.criar_busca_execucao(criadora.id, "tinyfish", [], status="na_fila")
    ok, motivo = agendamento.pode_disparar_agora(criadora.id)
    assert not ok
    assert "fila ou rodando" in motivo


# ------------------------------------------------------- fila e idempotencia


def test_scheduler_dispara_uma_vez_por_horario_mesmo_repetindo_o_ciclo(criadora):
    passado = agendamento.em_utc(agendamento.agora() - timedelta(hours=1))
    db.salvar_agendamento_busca(criadora.id, "diario", 9, 0, passado, ativo=1)
    with (
        patch.dict(os.environ, {"TINYFISH_API_KEY": "chave-teste"}),
        patch.object(fila_buscas, "enfileirar", return_value={"ok": True}) as enfileirar,
    ):
        primeiro = agendamento.processar_vencidos()
        segundo = agendamento.processar_vencidos()

    assert len(primeiro) == 1 and primeiro[0]["execucao_id"]
    assert segundo == []
    assert enfileirar.call_count == 1
    execucoes = db.listar_execucoes_criadora(criadora.id)
    assert len(execucoes) == 1
    assert execucoes[0]["origem"] == "agendada"
    assert execucoes[0]["status"] == "na_fila"


def test_restart_nao_reexecuta_a_mesma_linha(criadora):
    passado = agendamento.em_utc(agendamento.agora() - timedelta(hours=1))
    db.salvar_agendamento_busca(criadora.id, "diario", 9, 0, passado, ativo=1)
    with (
        patch.dict(os.environ, {"TINYFISH_API_KEY": "chave-teste"}),
        patch.object(fila_buscas, "enfileirar", return_value={"ok": True}),
    ):
        execucao_id = agendamento.processar_vencidos()[0]["execucao_id"]

    resumo = {"consultas": ["a"], "novos": 1, "erros": [], "erro": ""}
    with patch.object(worker.fluxo_busca, "executar", return_value=resumo) as executar:
        primeira = worker.executar_busca_agendada(criadora.id, execucao_id, "tinyfish")
        segunda = worker.executar_busca_agendada(criadora.id, execucao_id, "tinyfish")

    assert primeira["status"] == "concluida"
    assert segunda.get("ignorada") is True
    assert executar.call_count == 1


# ------------------------------------------------------------------- estados


def test_resultado_com_erro_parcial_fica_com_avisos(criadora):
    execucao_id = db.criar_busca_execucao(
        criadora.id, "tinyfish", [], status="na_fila"
    )
    resumo = {
        "consultas": ["a", "b"],
        "novos": 4,
        "erros": ["consulta b: TinyFish: limite de requisições atingido (HTTP 429)."],
        "erro": "",
        "erro_instagram": "",
    }
    with patch.object(worker.fluxo_busca, "executar", return_value=resumo):
        resultado = worker.executar_busca_agendada(criadora.id, execucao_id, "tinyfish")

    registro = db.obter_busca_execucao(execucao_id)
    assert resultado["status"] == "avisos"
    assert registro["status"] == "avisos"
    assert registro["motivo_falha"] == "limite_provedor"
    assert registro["duracao_ms"] is not None


def test_falha_guarda_o_motivo_402_e_instagram(criadora):
    primeira = db.criar_busca_execucao(criadora.id, "tinyfish", [], status="na_fila")
    segunda = db.criar_busca_execucao(criadora.id, "tinyfish", [], status="na_fila")
    with patch.object(
        worker.fluxo_busca,
        "executar",
        return_value={"erro": "TinyFish respondeu HTTP 402 sem saldo.", "erros": []},
    ):
        worker.executar_busca_agendada(criadora.id, primeira, "tinyfish")
    with patch.object(
        worker.fluxo_busca,
        "executar",
        return_value={"erro": "", "erros": [], "erro_instagram": "Instagram bloqueou."},
    ):
        worker.executar_busca_agendada(criadora.id, segunda, "tinyfish")

    assert db.obter_busca_execucao(primeira)["motivo_falha"] == "sem_creditos"
    assert db.obter_busca_execucao(segunda)["motivo_falha"] == "instagram_bloqueado"


def test_execucao_presa_e_fechada_como_interrompida(criadora):
    execucao_id = db.criar_busca_execucao(
        criadora.id, "tinyfish", [], status="rodando"
    )
    antigo = (datetime.now() - timedelta(hours=4)).isoformat()
    with db.conexao() as conn:
        conn.execute(
            "UPDATE busca_execucao SET iniciada_em = ? WHERE id = ?",
            (antigo, execucao_id),
        )
    assert db.marcar_execucoes_abandonadas(minutos=60) == 1
    registro = db.obter_busca_execucao(execucao_id)
    assert registro["status"] == "erro"
    assert registro["motivo_falha"] == "interrompida"


# ------------------------------------------------------ limite global 30/min


class _Relogio:
    def __init__(self):
        self.agora = 1000.0
        self.dormidas = []

    def ler(self):
        return self.agora

    def dormir(self, segundos):
        self.dormidas.append(segundos)
        self.agora += segundos


def test_duas_criadoras_juntas_respeitam_30_por_minuto():
    relogio = _Relogio()
    store = limite_taxa.StoreMemoria()
    # O mesmo store/chave para as duas: e o limitador global do TinyFish.
    criadora_a = limite_taxa.Limitador(store, dormir=relogio.dormir, relogio=relogio.ler)
    criadora_b = limite_taxa.Limitador(store, dormir=relogio.dormir, relogio=relogio.ler)

    momentos = []
    for indice in range(40):
        (criadora_a if indice % 2 == 0 else criadora_b).aguardar()
        momentos.append(relogio.agora)

    assert momentos[:30] == [1000.0] * 30
    assert momentos[30] > 1000.0
    for posicao, momento in enumerate(momentos):
        na_janela = [item for item in momentos[posicao:] if item - momento < 60.0]
        assert len(na_janela) <= 30
    assert relogio.dormidas, "o 31o pedido tinha que esperar a janela"


# --------------------------------------------------------------------- telas


def test_tela_de_perfis_mostra_agendamento_e_salva(criadora):
    pagina = criadora.cliente.get("/configuracoes/perfis")
    assert pagina.status_code == 200
    assert "Escaneamento agendado" in pagina.text
    assert "Escanear agora" in pagina.text

    token = _csrf(pagina.text)
    resposta = criadora.cliente.post(
        "/configuracoes/agendamento",
        data={
            "csrf_token": token,
            "frequencia": "semanal",
            "hora": "8",
            "dia_semana": "2",
        },
        follow_redirects=True,
    )
    assert "Agendamento salvo." in resposta.text
    registro = db.obter_agendamento_busca(criadora.id)
    assert (registro["frequencia"], registro["hora"], registro["dia_semana"]) == (
        "semanal",
        8,
        2,
    )


def test_escanear_agora_avisa_quando_a_fila_nao_esta_configurada(criadora):
    token = _csrf(criadora.cliente.get("/configuracoes/perfis").text)
    with patch.dict(os.environ, {"TINYFISH_API_KEY": "chave-teste"}):
        resposta = criadora.cliente.post(
            "/configuracoes/escanear-agora",
            data={"csrf_token": token},
            follow_redirects=True,
        )
    assert "REDIS_URL" in resposta.text
    execucao = db.ultima_execucao(criadora.id)
    assert execucao["status"] == "erro"
    assert execucao["motivo_falha"] == "fila"


def test_historico_mostra_so_as_execucoes_da_criadora(criadora):
    outra_id = db.criar_usuario(
        "outra_criadora",
        "outra@example.com",
        autenticacao.hash_senha(SENHA),
    )
    minha = db.criar_busca_execucao(
        criadora.id, "tinyfish", [], status="na_fila"
    )
    with patch.object(
        worker.fluxo_busca,
        "executar",
        return_value={"consultas": ["a", "b"], "novos": 3, "erros": [], "erro": ""},
    ):
        worker.executar_busca_agendada(criadora.id, minha, "tinyfish")
    alheia = db.criar_busca_execucao(outra_id, "serpapi", [], status="na_fila")
    with patch.object(
        worker.fluxo_busca,
        "executar",
        return_value={"consultas": ["x"], "novos": 9, "erros": [], "erro": ""},
    ):
        worker.executar_busca_agendada(outra_id, alheia, "serpapi")

    pagina = criadora.cliente.get("/configuracoes/historico")
    assert pagina.status_code == 200
    assert "TinyFish (web)" in pagina.text
    assert "SerpAPI (Google)" not in pagina.text
    assert "Concluída" in pagina.text
    assert "3" in pagina.text
