"""Avisos por e-mail, linha do tempo e exportacao de metadados."""

import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import autenticacao, avisos, remocao
from app.main import app

SENHA = "senha-teste-avisos"
URL_A = "https://site-exemplo.example/a/1"
URL_B = "https://site-exemplo.example/a/2"
SEGREDO = "TRECHO_QUE_NAO_PODE_VAZAR"


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
def ambiente(tmp_path):
    with patch.object(db, "CAMINHO_DB", tmp_path / "avisos.db"):
        db.iniciar_banco()
        criadora = db.criar_usuario(
            "criadora_avisos",
            "criadora_avisos@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Ana",
            public_handle="anaanax",
        )
        outra = db.criar_usuario(
            "outra_avisos",
            "outra_avisos@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Bia",
            public_handle="bia",
        )
        db.confirmar_email(criadora)
        db.confirmar_email(outra)
        db.adicionar_perfil_monitorado(criadora, "instagram", "anaanax")
        execucao = db.criar_busca_execucao(criadora, "tinyfish", ["ana"])
        db.salvar_candidatos_busca(
            criadora,
            execucao,
            [
                {
                    "url_normalizada": URL_A,
                    "dominio": "site-exemplo.example",
                    "titulo": "Ana album 1",
                    "trecho": SEGREDO,
                    "provedor": "tinyfish",
                    "nota": 90,
                    "classificacao": "provavel_vazamento",
                    "chave_grupo": "",
                },
                {
                    "url_normalizada": URL_B,
                    "dominio": "site-exemplo.example",
                    "titulo": "Ana album 2",
                    "trecho": SEGREDO,
                    "provedor": "tinyfish",
                    "nota": 80,
                    "classificacao": "provavel_vazamento",
                    "chave_grupo": "",
                },
                {
                    "url_normalizada": "https://outro.example/c/3",
                    "dominio": "outro.example",
                    "titulo": "Ana album 3",
                    "trecho": SEGREDO,
                    "provedor": "tinyfish",
                    "nota": 70,
                    "classificacao": "provavel_vazamento",
                    "chave_grupo": "",
                },
                {
                    "url_normalizada": "https://outro.example/c/4",
                    "dominio": "outro.example",
                    "titulo": "Ana album 4",
                    "trecho": SEGREDO,
                    "provedor": "tinyfish",
                    "nota": 60,
                    "classificacao": "provavel_vazamento",
                    "chave_grupo": "",
                },
            ],
        )
        yield {"criadora": criadora, "outra": outra, "execucao": execucao}


def _usuario(criadora_id):
    return db.obter_usuario_por_id(criadora_id)


def test_preferencias_ligam_e_desligam(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_avisos")
    pagina = cliente.get("/configuracoes/perfis")
    assert "Avisos por e-mail" in pagina.text
    token = _csrf(pagina.text)
    resposta = cliente.post(
        "/configuracoes/avisos",
        data={"csrf_token": token, "resumo_diario": "1"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    preferencias = db.obter_preferencia_aviso(ambiente["criadora"])
    assert preferencias["email_scan"] == 0
    assert preferencias["resumo_diario"] == 1
    assert preferencias["resumo_semanal"] == 0

    pagina = cliente.get("/configuracoes/perfis")
    trecho = pagina.text.split("Avisos por e-mail", 1)[1][:2500]
    assert 'name="email_scan"' in trecho
    assert 'name="resumo_diario" value="1"' in trecho


def test_email_de_novos_leva_so_dominio_titulo_e_link_seguro(ambiente):
    usuario = _usuario(ambiente["criadora"])
    top = db.candidatos_novos_da_execucao(ambiente["execucao"], 30)
    assunto, texto, corpo_html = avisos.montar_email_novos(usuario, len(top), top[:3])
    assert "4 novo" in assunto
    assert "site-exemplo.example" in texto and "outro.example" in texto
    assert SEGREDO not in texto and SEGREDO not in corpo_html
    assert len([linha for linha in texto.splitlines() if linha.startswith("- ")]) == 3
    link = avisos.url_da_fila()
    assert link.endswith("/revisao")
    assert "?" not in link
    assert link in texto and link in corpo_html


def test_notificar_novos_respeita_preferencia(ambiente):
    db.salvar_preferencia_aviso(ambiente["criadora"], False, False, False)
    with patch.object(avisos, "enviar_pendentes", return_value={}) as envio:
        assert avisos.notificar_novos(ambiente["criadora"], ambiente["execucao"]) is None
    assert envio.call_count == 0

    db.salvar_preferencia_aviso(ambiente["criadora"], True, False, False)
    with patch.object(avisos, "enviar_pendentes", return_value={}) as envio:
        aviso_id = avisos.notificar_novos(ambiente["criadora"], ambiente["execucao"])
    assert aviso_id and envio.call_count == 1
    avisos_usuario = db.listar_avisos_usuario(ambiente["criadora"])
    assert len(avisos_usuario) == 1
    assert "novo(s)" in avisos_usuario[0]["assunto"]


class _SmtpFalso:
    enviados = []

    def __init__(self, host, porta, timeout=20):
        self.host = host
        self.porta = porta

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        pass

    def login(self, usuario, senha):
        pass

    def send_message(self, mensagem):
        _SmtpFalso.enviados.append(mensagem)


def test_envio_smtp_marca_aviso_como_enviado(ambiente):
    db.criar_aviso(
        ambiente["criadora"],
        "novos",
        "teste:1",
        "1 novo resultado",
        "Olá,\n\n1 novo resultado para revisar.",
        "<p>1 novo resultado</p>",
    )
    _SmtpFalso.enviados = []
    with patch.dict(
        "os.environ",
        {
            "SMTP_HOST": "smtp.example",
            "SMTP_PORT": "587",
            "SMTP_FROM": "avisos@example.com",
            "SMTP_USER": "usuario",
            "SMTP_PASSWORD": "segredo",
        },
    ), patch.object(avisos.smtplib, "SMTP", _SmtpFalso):
        resultado = avisos.enviar_pendentes()
    assert resultado["enviados"] == 1
    assert len(_SmtpFalso.enviados) == 1
    mensagem = _SmtpFalso.enviados[0]
    assert mensagem["To"] == "criadora_avisos@example.com"
    assert SEGREDO not in mensagem.get_body("plain").get_content()
    avisos_usuario = db.listar_avisos_usuario(ambiente["criadora"])
    assert avisos_usuario[0]["enviado_em"]


def test_resumo_diario_nao_repete_no_mesmo_dia(ambiente):
    db.salvar_preferencia_aviso(ambiente["criadora"], False, True, False)
    with patch.object(avisos, "enviar_pendentes", return_value={}):
        base = datetime.now(timezone.utc)
        assert avisos.processar_resumos(base=base) == [ambiente["criadora"]]
        assert avisos.processar_resumos(base=base) == []
    tipos = [item["tipo"] for item in db.listar_avisos_usuario(ambiente["criadora"])]
    assert tipos.count("resumo_diario") == 1


def test_linha_do_tempo_mostra_as_quatro_etapas(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_avisos")
    candidato = db.obter_candidato(1, ambiente["criadora"])
    db.atualizar_status_candidato(candidato["id"], ambiente["criadora"], "leaked")
    db.adicionar_site_vazado(
        ambiente["criadora"],
        ambiente["criadora"],
        URL_A,
        "Ana album 1",
        "site-exemplo.example",
        SEGREDO,
        "",
        "tinyfish",
        {},
    )
    pedido_id = db.criar_pedido_remocao(
        ambiente["criadora"],
        "site-exemplo.example",
        "pt",
        "texto",
        "",
        "",
        "tinyfish",
        [{"url": URL_A, "titulo": "Ana album 1"}],
    )
    db.atualizar_pedido_status(ambiente["criadora"], pedido_id, "enviado")
    db.atualizar_pedido_status(ambiente["criadora"], pedido_id, "removido")

    pagina = cliente.get("/linha-do-tempo")
    assert pagina.status_code == 200
    for etapa in ("Detectado:", "Revisado:", "Enviado:", "Removido:"):
        assert etapa in pagina.text
    assert "site-exemplo.example" in pagina.text
    assert "Taxa de remoção" in pagina.text and "Sites reincidentes" in pagina.text
    assert SEGREDO not in pagina.text


def test_exportacoes_levam_so_metadados(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_avisos")
    csv = cliente.get("/linha-do-tempo.csv")
    assert csv.status_code == 200
    assert "text/csv" in csv.headers["content-type"]
    assert "attachment" in csv.headers["content-disposition"]
    assert URL_A in csv.text and "site-exemplo.example" in csv.text
    assert SEGREDO not in csv.text
    assert "trecho" not in csv.text.lower()

    pdf = cliente.get("/linha-do-tempo.pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-1.4")
    assert b"%%EOF" in pdf.content
    assert SEGREDO.encode() not in pdf.content


def test_exportacao_e_linha_do_tempo_exigem_login(ambiente):
    anonimo = TestClient(app)
    resposta = anonimo.get("/linha-do-tempo.csv", follow_redirects=False)
    assert resposta.status_code in (303, 307)
    assert "/login" in resposta.headers.get("location", "")


def test_outra_criadora_nao_ve_linha_do_tempo(ambiente):
    outra = TestClient(app)
    _entrar(outra, "outra_avisos")
    resposta = outra.get(
        f"/linha-do-tempo?target_user_id={ambiente['criadora']}",
        follow_redirects=False,
    )
    assert resposta.status_code == 403
