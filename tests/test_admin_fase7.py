"""Fase 7: admin, operacao, custo, abuso, exclusao e auditoria.

O aceite principal esta em dois testes: nenhuma tela admin responde a quem nao
e admin e a exclusao de conta apaga os dados, anonimiza a auditoria e derruba a
sessao.
"""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import autenticacao, perfis_monitorados, remocao
from app.main import app

SENHA = "senha-teste-fase-sete"


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


def _contar(tabela: str, coluna: str, valor: int) -> int:
    with db.conexao() as conn:
        linha = conn.execute(
            f"SELECT COUNT(*) AS total FROM {tabela} WHERE {coluna} = ?", (valor,)
        ).fetchone()
    return int(linha["total"] or 0)


@pytest.fixture()
def ambiente(tmp_path):
    with patch.object(db, "CAMINHO_DB", tmp_path / "fase7.db"):
        db.iniciar_banco()
        admin = db.criar_usuario(
            "admin_fase7",
            "admin_fase7@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Admin",
            public_handle="admin",
        )
        cliente = db.criar_usuario(
            "cliente_fase7",
            "cliente_fase7@example.com",
            autenticacao.hash_senha(SENHA),
            public_name="Cliente",
            public_handle="cliente",
        )
        sinal = db.criar_usuario(
            "sinal_fase7", "sinal_fase7@example.com", autenticacao.hash_senha(SENHA)
        )
        for user_id in (admin, cliente, sinal):
            db.confirmar_email(user_id)
        db.atualizar_perfil_usuario(admin, "admin")
        yield {"admin": admin, "cliente": cliente, "sinal": sinal}


def test_telas_admin_nao_abrem_para_cliente_nem_visitante(ambiente):
    telas = [
        "/admin",
        "/admin/uso",
        "/admin/planos",
        "/admin/modelos",
        "/admin/auditoria",
        "/dominios",
        "/contatos-abuso",
        "/usuarios",
    ]
    visitante = TestClient(app)
    for caminho in telas:
        resposta = visitante.get(caminho, follow_redirects=False)
        assert resposta.status_code == 303, caminho
        assert resposta.headers["location"].startswith("/login"), caminho

    cliente = TestClient(app)
    _entrar(cliente, "cliente_fase7")
    for caminho in telas:
        assert cliente.get(caminho).status_code == 403, caminho

    admin = TestClient(app)
    _entrar(admin, "admin_fase7")
    for caminho in telas:
        assert admin.get(caminho).status_code == 200, caminho


def test_admin_mostra_sinais_de_abuso_sem_bloquear(ambiente):
    for _ in range(db.MAX_TENTATIVAS_LOGIN):
        db.registrar_tentativa_login("sinal_fase7", "203.0.113.10")
    execucao = db.criar_busca_execucao(
        ambiente["cliente"], "tinyfish", ["cliente"], status="erro"
    )
    db.finalizar_busca_execucao(
        execucao, status="erro", motivo_falha="limite_provedor"
    )
    sinais = db.sinais_abuso()
    tipos = {sinal["tipo"] for sinal in sinais}
    assert "login" in tipos
    assert "falhas_busca" not in tipos  # uma falha sozinha nao alerta

    admin = TestClient(app)
    _entrar(admin, "admin_fase7")
    pagina = admin.get("/admin")
    assert pagina.status_code == 200
    assert "Sinais de abuso" in pagina.text
    assert "Só alerta" in pagina.text
    # O alerta sozinho nao bloqueia: limpo as tentativas simuladas e a conta entra.
    db.limpar_tentativas_login("sinal_fase7", "203.0.113.10")
    sinal = TestClient(app)
    _entrar(sinal, "sinal_fase7")


def test_uso_por_criadora_dia_e_falhas(ambiente):
    for _ in range(2):
        execucao = db.criar_busca_execucao(
            ambiente["cliente"], "tinyfish", ["cliente"]
        )
        db.finalizar_busca_execucao(
            execucao, status="erro", motivo_falha="instagram_bloqueado"
        )
    uso = db.uso_buscas(7)
    criadora = next(
        item for item in uso["criadoras"] if item["criadora_id"] == ambiente["cliente"]
    )
    assert criadora["total"] == 2
    assert criadora["hoje"] == 2
    assert criadora["falhas"] == 2
    assert criadora["limite_dia"] == db.limite_buscas_por_dia(ambiente["cliente"])
    assert uso["falhas_por_tipo"] == [{"tipo": "instagram_bloqueado", "total": 2}]
    assert sum(uso["total_por_dia"].values()) == 2

    admin = TestClient(app)
    _entrar(admin, "admin_fase7")
    pagina = admin.get("/admin/uso?dias=7")
    assert pagina.status_code == 200
    assert "Cliente" in pagina.text
    assert "instagram_bloqueado" in pagina.text


def test_planos_editaveis_e_aplicados_ao_limite(ambiente):
    admin = TestClient(app)
    _entrar(admin, "admin_fase7")
    token = _csrf(admin.get("/admin/planos").text)
    resposta = admin.post(
        "/admin/planos",
        data={
            "csrf_token": token,
            "plano": "gratis",
            "nome": "Gratuito",
            "max_perfis": "1",
            "buscas_dia": "1",
            "frequencia_horas": "24",
            "descricao": "Limite de teste.",
        },
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert db.obter_plano("gratis")["max_perfis"] == 1
    assert db.limite_perfis_monitorados(ambiente["cliente"]) == 1
    assert db.limite_buscas_por_dia(ambiente["cliente"]) == 1

    assert perfis_monitorados.salvar_perfil(
        ambiente["cliente"], "instagram", "cliente"
    )
    with pytest.raises(ValueError):
        perfis_monitorados.salvar_perfil(ambiente["cliente"], "tiktok", "cliente")

    assert any(
        evento["acao"] == "plano_alterado"
        for evento in db.listar_auditoria(20)
    )


def test_exclusao_de_conta_apaga_dados_e_anonimiza_auditoria(ambiente):
    cliente_id = ambiente["cliente"]
    db.adicionar_perfil_monitorado(cliente_id, "instagram", "cliente")
    db.adicionar_variante_nome(cliente_id, "cliente variacao")
    execucao_id = db.criar_busca_execucao(cliente_id, "tinyfish", ["cliente"])
    with db.conexao() as conn:
        conn.execute(
            """
            INSERT INTO search_candidates
                (user_id, url, title, source, snippet, thumbnail, engine, status)
            VALUES (?, 'https://exemplo.example/cliente', 'Cliente', '', '', '',
                    'tinyfish', 'pending')
            """,
            (cliente_id,),
        )
        conn.execute(
            """
            INSERT INTO pedido_remocao (user_id, dominio, status)
            VALUES (?, 'exemplo.example', 'a_enviar')
            """,
            (cliente_id,),
        )

    cliente = TestClient(app)
    _entrar(cliente, "cliente_fase7")
    token = _csrf(cliente.get("/configuracoes/conta").text)

    errada = cliente.post(
        "/configuracoes/conta/excluir",
        data={"csrf_token": token, "senha": "senha-errada", "confirmacao": "EXCLUIR"},
        follow_redirects=False,
    )
    assert errada.status_code == 303
    assert db.obter_usuario_por_id(cliente_id) is not None

    assert cliente.post(
        "/configuracoes/conta/excluir",
        data={"csrf_token": token, "senha": SENHA, "confirmacao": "excluir"},
        follow_redirects=False,
    ).status_code == 303
    assert db.obter_usuario_por_id(cliente_id) is not None  # palavra exata

    resposta = cliente.post(
        "/configuracoes/conta/excluir",
        data={"csrf_token": token, "senha": SENHA, "confirmacao": "EXCLUIR"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert resposta.headers["location"].startswith("/login")

    assert db.obter_usuario_por_id(cliente_id) is None
    assert _contar("search_candidates", "user_id", cliente_id) == 0
    assert _contar("perfil_monitorado", "criadora_id", cliente_id) == 0
    assert _contar("nome_variante", "criadora_id", cliente_id) == 0
    assert _contar("busca_execucao", "criadora_id", cliente_id) == 0
    assert _contar("pedido_remocao", "user_id", cliente_id) == 0
    assert _contar("auth_sessions", "user_id", cliente_id) == 0

    eventos = [
        evento
        for evento in db.listar_auditoria(50)
        if evento["acao"] == "conta_excluida"
    ]
    assert eventos
    assert eventos[0]["actor_user_id"] is None
    assert eventos[0]["actor_label"] == ""
    assert eventos[0]["alvo_id"] == ""

    # A sessao antiga nao volta a autenticar.
    assert cliente.get("/configuracoes/conta", follow_redirects=False).status_code == 303


def test_login_bloqueia_apos_cinco_falhas_e_limpa_ao_entrar(ambiente):
    cliente = TestClient(app)
    token = _csrf(cliente.get("/login").text)
    for _ in range(db.MAX_TENTATIVAS_LOGIN):
        resposta = cliente.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "cliente_fase7",
                "senha": "senha-errada",
                "next": "/",
            },
        )
        assert resposta.status_code == 400

    bloqueado = cliente.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "cliente_fase7",
            "senha": SENHA,
            "next": "/",
        },
    )
    assert bloqueado.status_code == 429
    assert "Muitas tentativas" in bloqueado.text
    assert any(
        evento["acao"] == "login_bloqueado" for evento in db.listar_auditoria(20)
    )

    db.limpar_tentativas_login("cliente_fase7", "testclient")
    _entrar(TestClient(app), "cliente_fase7")


def test_exportacao_e_dominio_ficam_na_auditoria(ambiente):
    cliente = TestClient(app)
    _entrar(cliente, "cliente_fase7")
    assert cliente.get("/linha-do-tempo.csv").status_code == 200
    assert any(
        evento["acao"] == "exportacao"
        and evento["alvo_id"] == str(ambiente["cliente"])
        for evento in db.listar_auditoria(20)
    )

    admin = TestClient(app)
    _entrar(admin, "admin_fase7")
    token = _csrf(admin.get("/dominios?status=sugerido").text)
    db.registrar_sugestao_dominio(
        "auditado.example", 80, ["teste"], "https://auditado.example/a", {}
    )
    resposta = admin.post(
        "/dominios/auditado.example/status",
        data={"csrf_token": token, "acao": "rejeitar"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert any(
        evento["acao"] == "dominio_status" and evento["alvo_id"] == "auditado.example"
        for evento in db.listar_auditoria(20)
    )


def test_modelo_de_texto_editavel_e_restauravel(ambiente):
    admin = TestClient(app)
    _entrar(admin, "admin_fase7")
    token = _csrf(admin.get("/admin/modelos").text)
    resposta = admin.post(
        "/admin/modelos",
        data={
            "csrf_token": token,
            "codigo": "pt",
            "titulo": "Modelo de teste",
            "conteudo": "MODELO-FASE7 {{nome}} (@{{arroba}}):\n{{urls}}",
        },
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert "MODELO-FASE7" in remocao.carregar_modelo("pt")
    assert "MODELO-FASE7" in remocao.gerar_texto(
        "pt", nome="Ana", arroba="ana", urls=["https://exemplo.example/a"]
    )
    assert any(
        evento["acao"] == "modelo_texto" for evento in db.listar_auditoria(20)
    )

    assert admin.post(
        "/admin/modelos/pt/restaurar",
        data={"csrf_token": token},
        follow_redirects=False,
    ).status_code == 303
    assert "MODELO-FASE7" not in remocao.carregar_modelo("pt")


def test_purga_respeita_retencao_de_doze_meses(ambiente):
    db.registrar_auditoria("exportacao", detalhe="evento antigo")
    antigo = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    with db.conexao() as conn:
        conn.execute(
            "UPDATE auditoria_evento SET criado_em = ? WHERE detalhe = 'evento antigo'",
            (antigo,),
        )
    assert db.purgar_auditoria() == 1
    assert all(
        evento["detalhe"] != "evento antigo" for evento in db.listar_auditoria(20)
    )


def test_cabecalhos_de_seguranca_e_sessao():
    cliente = TestClient(app)
    resposta = cliente.get("/login")
    assert resposta.headers["X-Content-Type-Options"] == "nosniff"
    assert resposta.headers["X-Frame-Options"] == "DENY"
    assert resposta.headers["Referrer-Policy"] == "same-origin"
    assert "frame-ancestors 'none'" in resposta.headers["Content-Security-Policy"]
    assert resposta.headers["Cache-Control"] == "no-store"
    cookie = resposta.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
