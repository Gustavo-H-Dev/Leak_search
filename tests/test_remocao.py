"""Kit de remocao: contato do site, texto, status, historico e checagem."""

import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import autenticacao, busca_provedores, remocao
from app.main import app

SENHA = "senha-teste-remocao"
URL_A = "https://site-exemplo.example/a/1"
URL_B = "https://site-exemplo.example/a/2"
URL_OUTRA = "https://outro-site.example/a/9"


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
    with patch.object(db, "CAMINHO_DB", tmp_path / "remocao.db"):
        db.iniciar_banco()
        criadora = db.criar_usuario(
            "criadora_remocao",
            "criadora_remocao@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Ana",
            public_handle="anaanax",
        )
        outra = db.criar_usuario(
            "outra_criadora",
            "outra_criadora@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Bia",
            public_handle="bia",
        )
        admin = db.criar_usuario(
            "admin_remocao",
            "admin_remocao@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Admin",
        )
        for usuario in (criadora, outra, admin):
            db.confirmar_email(usuario)
        db.atualizar_perfil_usuario(admin, "admin")
        for url, titulo in ((URL_A, "Ana album 1"), (URL_B, "Ana album 2")):
            db.adicionar_site_vazado(
                criadora,
                criadora,
                url,
                titulo,
                "site-exemplo.example",
                "trecho",
                "",
                "tinyfish",
                {},
            )
        db.adicionar_site_vazado(
            outra, outra, URL_OUTRA, "Bia album", "outro-site.example", "", "", "tinyfish", {}
        )
        yield {"criadora": criadora, "outra": outra, "admin": admin}


def _criar_pedido(cliente, urls=(URL_A, URL_B), modelo="pt", notas=""):
    token = _csrf(cliente.get(f"/remocao/novo?url={URL_A}").text)
    dados = {
        "csrf_token": token,
        "target_user_id": 0,
        "url": URL_A,
        "modelo": modelo,
        "nome": "Ana",
        "arroba": "anaanax",
        "descricao": "Conteúdo publicado sem autorização.",
        "urls": list(urls),
        "notas": notas,
    }
    resposta = cliente.post("/remocao", data=dados, follow_redirects=False)
    assert resposta.status_code == 303, resposta.text
    return int(resposta.headers["location"].split("/remocao/")[1].split("?")[0])


def test_modelos_pt_e_en_nao_inventam_base_legal():
    texto_pt = remocao.gerar_texto(
        "pt", nome="Ana", arroba="@anaanax", urls=[URL_A], descricao="minha descrição"
    )
    assert "Ana" in texto_pt and "@anaanax" in texto_pt and URL_A in texto_pt
    assert "minha descrição" in texto_pt
    assert "{{" not in texto_pt
    assert "advogado" not in texto_pt.lower()

    texto_en = remocao.gerar_texto("en", nome="Ana", arroba="anaanax", urls=[URL_A])
    assert "removal" in texto_en.lower()
    assert URL_A in texto_en
    assert "{{" not in texto_en


def test_sem_contato_mostra_cadastre_e_texto_para_copiar(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    resposta = cliente.get(f"/remocao/novo?url={URL_A}")
    assert resposta.status_code == 200
    assert "Cadastre o contato deste site" in resposta.text
    assert "Copiar texto" in resposta.text
    assert "Modelo a ser revisado por advogado" in resposta.text
    assert URL_A in resposta.text and URL_B in resposta.text
    assert "content_removal_form" in resposta.text


def test_admin_cadastra_contato_e_criadora_ve_email(ambiente):
    admin = TestClient(app)
    _entrar(admin, "admin_remocao")
    token = _csrf(admin.get("/contatos-abuso").text)
    resposta = admin.post(
        "/contatos-abuso",
        data={
            "csrf_token": token,
            "dominio": "site-exemplo.example",
            "email": "abuso@site-exemplo.example",
            "url_formulario": "https://site-exemplo.example/abuso",
            "observacao": "pede o link completo",
            "dias": "4",
        },
        follow_redirects=False,
    )
    assert resposta.status_code == 303

    criadora = TestClient(app)
    _entrar(criadora, "criadora_remocao")
    pagina = criadora.get(f"/remocao/novo?url={URL_A}")
    assert "abuso@site-exemplo.example" in pagina.text
    assert "pede o link completo" in pagina.text
    assert remocao.dias_checagem() == 4


def test_pedido_cobre_varios_links_do_mesmo_dominio(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    pedido_id = _criar_pedido(cliente)
    pedido = db.obter_pedido_remocao(ambiente["criadora"], pedido_id)
    assert [item["url"] for item in pedido["itens"]] == [URL_A, URL_B]
    assert pedido["texto"].count(URL_A) == 1 and pedido["texto"].count(URL_B) == 1
    situacoes = db.situacoes_leaks(ambiente["criadora"])
    assert situacoes[URL_A] == "em_remocao" and situacoes[URL_B] == "em_remocao"
    assert pedido["eventos"][0]["status"] == "criado"


def test_enviado_e_removido_ficam_no_historico_e_no_relatorio(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    pedido_id = _criar_pedido(cliente)
    token = _csrf(cliente.get(f"/remocao/{pedido_id}").text)

    enviado = cliente.post(
        f"/remocao/{pedido_id}/status",
        data={
            "csrf_token": token,
            "target_user_id": 0,
            "status": "enviado",
            "nota": "protocolo 123",
        },
        follow_redirects=False,
    )
    assert enviado.status_code == 303
    pedido = db.obter_pedido_remocao(ambiente["criadora"], pedido_id)
    assert pedido["status"] == "enviado"
    assert pedido["enviado_em"] and pedido["proxima_checagem_em"]

    pagina = cliente.get(f"/remocao/{pedido_id}")
    assert "protocolo 123" in pagina.text
    assert "Enviado" in pagina.text

    removido = cliente.post(
        f"/remocao/{pedido_id}/status",
        data={
            "csrf_token": token,
            "target_user_id": 0,
            "status": "removido",
            "nota": "site confirmou",
        },
        follow_redirects=False,
    )
    assert removido.status_code == 303
    pedido = db.obter_pedido_remocao(ambiente["criadora"], pedido_id)
    assert pedido["status"] == "removido"
    assert [evento["status"] for evento in pedido["eventos"]] == [
        "criado",
        "enviado",
        "removido",
    ]
    assert db.situacoes_leaks(ambiente["criadora"])[URL_A] == "removido"


def test_recusado_volta_o_link_para_o_ar(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    pedido_id = _criar_pedido(cliente)
    token = _csrf(cliente.get(f"/remocao/{pedido_id}").text)
    cliente.post(
        f"/remocao/{pedido_id}/status",
        data={"csrf_token": token, "target_user_id": 0, "status": "recusado"},
        follow_redirects=False,
    )
    assert db.situacoes_leaks(ambiente["criadora"])[URL_A] == "no_ar"


def test_checagem_ausente_sugere_removido_sem_mudar_sozinha(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    pedido_id = _criar_pedido(cliente, urls=(URL_A,))
    token = _csrf(cliente.get(f"/remocao/{pedido_id}").text)
    cliente.post(
        f"/remocao/{pedido_id}/status",
        data={"csrf_token": token, "target_user_id": 0, "status": "enviado"},
        follow_redirects=False,
    )

    with patch.object(busca_provedores, "buscar", return_value=[]) as busca:
        resposta = cliente.post(
            f"/remocao/{pedido_id}/checar",
            data={"csrf_token": token, "target_user_id": 0},
            follow_redirects=True,
        )
    assert resposta.status_code == 200
    assert busca.call_count == 1
    assert "Talvez já tenha saído do ar" in resposta.text
    pedido = db.obter_pedido_remocao(ambiente["criadora"], pedido_id)
    assert pedido["checagem_resultado"] == "ausente"
    assert pedido["sugestao_removido"] == 1
    assert pedido["status"] == "enviado"


def test_checagem_presente_nao_sugere_remocao(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    pedido_id = _criar_pedido(cliente, urls=(URL_A,))
    token = _csrf(cliente.get(f"/remocao/{pedido_id}").text)
    cliente.post(
        f"/remocao/{pedido_id}/status",
        data={"csrf_token": token, "target_user_id": 0, "status": "enviado"},
        follow_redirects=False,
    )
    achado = busca_provedores.Candidato(
        url=URL_A, dominio="site-exemplo.example", titulo="Ana", trecho=""
    )
    with patch.object(busca_provedores, "buscar", return_value=[achado]):
        cliente.post(
            f"/remocao/{pedido_id}/checar",
            data={"csrf_token": token, "target_user_id": 0},
            follow_redirects=True,
        )
    pedido = db.obter_pedido_remocao(ambiente["criadora"], pedido_id)
    assert pedido["checagem_resultado"] == "presente"
    assert pedido["sugestao_removido"] == 0


def test_checagem_exige_pedido_enviado(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    pedido_id = _criar_pedido(cliente)
    token = _csrf(cliente.get(f"/remocao/{pedido_id}").text)
    with patch.object(busca_provedores, "buscar", return_value=[]) as busca:
        resposta = cliente.post(
            f"/remocao/{pedido_id}/checar",
            data={"csrf_token": token, "target_user_id": 0},
            follow_redirects=True,
        )
    assert busca.call_count == 0
    assert "Marque o pedido como enviado antes de checar" in resposta.text


def test_outra_criadora_nao_abre_pedido_nem_link(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    pedido_id = _criar_pedido(cliente)

    outra = TestClient(app)
    _entrar(outra, "outra_criadora")
    assert outra.get(f"/remocao/{pedido_id}").status_code == 404
    assert outra.get(f"/remocao/novo?url={URL_A}").status_code == 404
    assert outra.get("/remocao").status_code == 200


def test_prazo_de_checagem_configuravel(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "criadora_remocao")
    pedido_id = _criar_pedido(cliente)
    token = _csrf(cliente.get(f"/remocao/{pedido_id}").text)
    db.salvar_configuracao("remocao_dias_checagem", "3")
    cliente.post(
        f"/remocao/{pedido_id}/status",
        data={"csrf_token": token, "target_user_id": 0, "status": "enviado"},
        follow_redirects=False,
    )
    pedido = db.obter_pedido_remocao(ambiente["criadora"], pedido_id)
    quando = datetime.fromisoformat(pedido["proxima_checagem_em"])
    dias = (quando - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 2.9 <= dias <= 3.1
