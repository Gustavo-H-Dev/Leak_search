"""Design system: layout base, componentes, telas de acesso e vitrine de dev.

Cobre o que a Fase 0 promete sem tocar em regra de negocio: assets servidos,
navegacao por papel, login/cadastro com os mesmos campos e a vitrine restrita
ao ambiente de desenvolvimento.
"""

import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.core import autenticacao
from app.main import app

SENHA = "senha-teste-design"


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
    with patch.object(db, "CAMINHO_DB", tmp_path / "design.db"):
        db.iniciar_banco()
        admin = db.criar_usuario(
            "admin_design",
            "admin_design@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Ana",
            public_handle="ana",
        )
        cliente = db.criar_usuario(
            "cliente_design",
            "cliente_design@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Bia",
            public_handle="bia",
        )
        db.confirmar_email(admin)
        db.confirmar_email(cliente)
        db.atualizar_perfil_usuario(admin, "admin")
        yield {"admin": admin, "cliente": cliente}


def _itens_da_barra_inferior(html: str) -> list[str]:
    trecho = html.split('data-nav="inferior"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'class="ds-nav-item"', trecho)


def test_css_e_js_do_design_system_carregam_para_visitante():
    visitante = TestClient(app)
    for caminho, tipo in (
        ("/static/css/app.css", "text/css"),
        ("/static/css/legado.css", "text/css"),
        ("/static/js/ui.js", "javascript"),
    ):
        resposta = visitante.get(caminho)
        assert resposta.status_code == 200, caminho
        assert tipo in resposta.headers["content-type"], caminho
        assert resposta.text.strip(), caminho


def test_login_usa_componentes_do_design_system_sem_mudar_os_campos():
    pagina = TestClient(app).get("/login")
    assert pagina.status_code == 200
    assert "ds-input" in pagina.text
    assert "ds-btn" in pagina.text
    # O formulario continua o mesmo: campos, action e CSRF.
    assert 'action="/login"' in pagina.text
    assert 'name="username"' in pagina.text
    assert 'name="senha"' in pagina.text
    assert 'name="csrf_token"' in pagina.text
    # A navegacao antiga (hamburguer) nao existe mais.
    assert "menu-toggle" not in pagina.text


def test_cadastro_mantem_ids_e_verificacao_do_instagram():
    pagina = TestClient(app).get("/cadastro")
    assert pagina.status_code == 200
    for marcador in (
        'id="form-cadastro"',
        'id="register-username"',
        'id="register-handle"',
        'id="register-password"',
        'id="btn-cadastrar"',
        'id="instagram-arroba"',
        'id="btn-verificar-instagram"',
        'id="modal-instagram"',
        'id="modal-btn-sim"',
        'id="csrf-token-instagram"',
    ):
        assert marcador in pagina.text, marcador
    assert "Verificar Instagram" in pagina.text


def test_admin_tem_cinco_itens_na_barra_inferior(ambiente):
    admin = TestClient(app)
    _entrar(admin, "admin_design")
    pagina = admin.get("/")
    assert pagina.status_code == 200
    assert len(_itens_da_barra_inferior(pagina.text)) == 5
    assert 'data-nav="inferior"' in pagina.text
    for destino in ('href="/valida"', 'href="/execucoes"', 'href="/perfil-busca"'):
        assert destino in pagina.text, destino
    assert 'aria-current="page"' in pagina.text


def test_cliente_nao_ve_historico_de_execucoes(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "cliente_design")
    pagina = cliente.get("/")
    assert pagina.status_code == 200
    assert len(_itens_da_barra_inferior(pagina.text)) == 4
    assert "/execucoes" not in pagina.text


def test_vitrine_de_componentes_so_existe_em_desenvolvimento(ambiente):
    admin = TestClient(app)
    _entrar(admin, "admin_design")
    with patch.object(main, "COMPONENTES_DEV_ATIVO", False):
        assert admin.get("/dev/componentes").status_code == 404
    with patch.object(main, "COMPONENTES_DEV_ATIVO", True):
        pagina = admin.get("/dev/componentes")
    assert pagina.status_code == 200
    for componente in (
        "ds-btn--perigo",
        "ds-badge--alerta",
        "ds-tabela",
        "ds-estado-vazio",
        "ds-skeleton",
        "ds-toast",
        "data-abrir-modal",
        'data-rotulo="Modelo"',
    ):
        assert componente in pagina.text, componente


def test_vitrine_de_componentes_exige_admin(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "cliente_design")
    with patch.object(main, "COMPONENTES_DEV_ATIVO", True):
        assert cliente.get("/dev/componentes").status_code == 403
