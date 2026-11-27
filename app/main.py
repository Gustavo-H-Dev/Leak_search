import json
import logging
import mimetypes
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

from app import db
from app.core import (
    agendamento,
    autenticacao,
    busca_provedores,
    busca_web,
    analise,
    candidatos as candidatos_core,
    consultas_vazamento,
    dominios_suspeitos as dominios_core,
    fluxo_busca,
    imagens,
    instagram,
    perfis_monitorados as perfis_core,
    pontuacao,
    provedores_busca,
    serpapi,
    sites_oficiais,
    tinyfish,
)
from app.core.middleware_auth import AutenticacaoMiddleware

PASTA_OFICIAIS = Path(os.getenv("MEDIA_DIR", "media/oficiais"))
PASTA_FOTOS_BUSCA = Path(
    os.getenv(
        "USER_SEARCH_MEDIA_DIR",
        str(PASTA_OFICIAIS.parent / "fotos_busca"),
    )
)
EXTENSOES_PERMITIDAS = {".jpg", ".jpeg", ".png", ".webp"}
TAMANHO_MAXIMO = 20 * 1024 * 1024  # 20 MB
logger = logging.getLogger(__name__)

# Vitrine dos componentes em /dev/componentes. Fica desligada por padrao: em
# producao nenhuma variavel e definida, entao a rota responde 404.
COMPONENTES_DEV_ATIVO = os.getenv("APP_ENV", "").strip().lower() in {
    "dev",
    "development",
    "local",
} or os.getenv("DEV_COMPONENTES", "").strip().lower() in {"1", "true", "yes"}

PASTA_OFICIAIS.mkdir(parents=True, exist_ok=True)
PASTA_FOTOS_BUSCA.mkdir(parents=True, exist_ok=True)
db.iniciar_banco()


def _remover_criadoras_antigas():
    for arquivo_antigo in db.remover_criadoras_sem_conta():
        if Path(arquivo_antigo).name != arquivo_antigo:
            logger.error("Arquivo antigo com caminho inválido não foi removido: %s", arquivo_antigo)
            continue
        try:
            (PASTA_OFICIAIS / arquivo_antigo).unlink(missing_ok=True)
        except OSError:
            logger.exception("Não foi possível remover arquivo antigo: %s", arquivo_antigo)


_remover_criadoras_antigas()

app = FastAPI(title="Monitoramento de Vazamentos")
session_secret = db.obter_segredo_sessao()
app.add_middleware(AutenticacaoMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="leak_search_session",
    max_age=14 * 24 * 60 * 60,
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"},
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _contexto_template(request: Request):
    session = request.scope.get("session", {})
    return {
        "csrf_token": session.get("csrf_token", ""),
        "current_user": getattr(request.state, "user", None),
    }


templates = Jinja2Templates(
    directory="app/templates",
    context_processors=[_contexto_template],
)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Gustavo.souza").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()
ADMIN_INITIAL_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", "")
if ADMIN_INITIAL_PASSWORD:
    email_admin = autenticacao.validar_email(ADMIN_EMAIL)
    db.criar_admin_se_ausente(
        ADMIN_USERNAME,
        email_admin,
        autenticacao.hash_senha(ADMIN_INITIAL_PASSWORD),
    )
elif not db.obter_usuario_por_login(ADMIN_USERNAME):
    logger.warning(
        "Conta admin ainda não criada. Configure ADMIN_EMAIL e ADMIN_INITIAL_PASSWORD."
    )


async def validar_csrf(request: Request):
    esperado = request.session.get("csrf_token", "")

    # Tenta o header X-CSRFToken primeiro (usado por fetch com headers)
    enviado = request.headers.get("x-csrftoken")

    # Se nao tiver header, tenta ler do corpo do formulário (FormData)
    if not enviado:
        try:
            formulario = await request.form()
            enviado = formulario.get("csrf_token")
        except Exception:
            enviado = None

    logger.info(f"CSRF validation: enviado={bool(enviado)}, esperado={bool(esperado)}, session_keys={list(request.session.keys())}")
    if not isinstance(enviado, str) or not secrets.compare_digest(enviado, esperado):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")


def _csrf():
    return [Depends(validar_csrf)]


def exigir_admin(request: Request):
    usuario = request.state.user
    if not usuario or usuario["role"] != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores.")


def _admin_csrf():
    return [Depends(exigir_admin), Depends(validar_csrf)]


@app.get("/arquivos/{nome_arquivo}", dependencies=[Depends(exigir_admin)])
def obter_arquivo_catalogo(nome_arquivo: str):
    if Path(nome_arquivo).name != nome_arquivo:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    caminho = PASTA_OFICIAIS / nome_arquivo
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    return FileResponse(caminho, media_type=mimetypes.guess_type(nome_arquivo)[0])


def _usuario_alvo_busca(request: Request, usuario_id: int = 0):
    atual = request.state.user
    if atual["role"] != "admin":
        if usuario_id and usuario_id != atual["id"]:
            raise HTTPException(
                status_code=403,
                detail="Clientes só podem pesquisar a própria conta.",
            )
        usuario_id = atual["id"]
    elif not usuario_id:
        usuario_id = atual["id"]

    alvo = db.obter_usuario_por_id(usuario_id)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    return alvo


def _proximo_caminho(valor: str) -> str:
    if valor.startswith("/") and not valor.startswith("//") and not re.search(r"[\r\n]", valor):
        return valor
    return "/"


def _nova_sessao(request: Request, user_id: int):
    token = secrets.token_urlsafe(32)
    expira = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    db.criar_sessao(autenticacao.hash_token(token), user_id, expira)
    request.session.clear()
    request.session["auth_session"] = token
    request.session["csrf_token"] = secrets.token_urlsafe(32)


def _url_aplicacao(path: str, **parametros: str) -> str:
    base_url = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    return f"{base_url}{path}?{urlencode(parametros)}"


def _criar_token_email(user_id: int, finalidade: str, validade: timedelta):
    token, token_hash, expira = autenticacao.gerar_token(validade)
    db.salvar_token_auth(token_hash, user_id, finalidade, expira)
    return token


async def _enviar_email_token(
    user_id: int,
    email: str,
    username: str,
    finalidade: str,
):
    if finalidade == "confirmar_email":
        token = _criar_token_email(
            user_id, finalidade, timedelta(hours=24)
        )
        link = _url_aplicacao("/confirmar-email", token=token)
        assunto = "Confirme seu e-mail"
        corpo = (
            f"Olá, {username}.\n\n"
            "Para confirmar seu endereço de e-mail, abra este link:\n"
            f"{link}\n\nO link expira em 24 horas."
        )
    else:
        token = _criar_token_email(user_id, finalidade, timedelta(hours=1))
        link = _url_aplicacao("/redefinir-senha", token=token)
        assunto = "Redefinição de senha"
        corpo = (
            f"Olá, {username}.\n\n"
            "Para definir uma nova senha, abra este link:\n"
            f"{link}\n\nO link expira em 1 hora. "
            "Se você não solicitou isso, ignore esta mensagem."
        )
    await run_in_threadpool(autenticacao.enviar_email, email, assunto, corpo)


@app.get("/login", response_class=HTMLResponse)
def login_formulario(
    request: Request,
    next: str = "/",
    erro: str = "",
    mensagem: str = "",
):
    if request.state.user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "erro": erro,
            "next": _proximo_caminho(next),
            "mensagem": (
                "Senha redefinida. Entre com sua nova senha."
                if mensagem == "senha_alterada"
                else mensagem
            ),
        },
    )


@app.post("/login", response_class=HTMLResponse, dependencies=_csrf())
async def login(
    request: Request,
    username: str = Form(...),
    senha: str = Form(...),
    next: str = Form("/"),
):
    usuario = db.obter_usuario_por_login(username.strip())
    if not usuario or not autenticacao.verificar_senha(usuario["password_hash"], senha):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "erro": "Usuário ou senha incorretos.",
                "mensagem": "",
                "next": _proximo_caminho(next),
            },
            status_code=400,
        )
    if not usuario["email_verified"] and autenticacao.confirmacao_email_obrigatoria():
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "erro": "Confirme seu e-mail antes de entrar.",
                "mensagem": "",
                "next": _proximo_caminho(next),
            },
            status_code=403,
        )
    if not usuario["email_verified"]:
        db.confirmar_email(usuario["id"])
    if autenticacao.password_hasher.check_needs_rehash(usuario["password_hash"]):
        db.atualizar_senha(
            usuario["id"], autenticacao.hash_senha(senha)
        )
    _nova_sessao(request, usuario["id"])
    return RedirectResponse(_proximo_caminho(next), status_code=303)


@app.get("/cadastro", response_class=HTMLResponse)
def cadastro_formulario(request: Request, erro: str = "", mensagem: str = ""):
    return templates.TemplateResponse(
        request,
        "cadastro.html",
        {
            "erro": erro,
            "mensagem": mensagem,
            "confirmacao_email_obrigatoria": autenticacao.confirmacao_email_obrigatoria(),
        },
    )


@app.post("/verificar-instagram")
async def verificar_instagram(request: Request, arroba: str = Form(...), csrf_token: str = Form(...)):
    """Verifica perfil Instagram. CSRF validado manualmente para garantir JSON"""
    logger.info(f"Verificar instagram: arroba={arroba}, csrf={bool(csrf_token)}")
    logger.info(f"DEBUG: Content-Type response will be: application/json")
    try:
        # Validar CSRF manualmente (garante resposta JSON em caso de erro)
        esperado = request.session.get("csrf_token", "")
        logger.info(f"DEBUG CSRF: enviado={bool(csrf_token)}, esperado={bool(esperado)}")
        if not isinstance(csrf_token, str) or not secrets.compare_digest(csrf_token, esperado):
            logger.info(f"DEBUG: CSRF validation FAILED - returning JSON error")
            return JSONResponse(
                {"ok": False, "erro": "Token CSRF inválido."},
                status_code=403
            )
        
        logger.info(f"DEBUG: CSRF validation OK - proceeding")
        usuario = serpapi.validar_usuario_instagram(arroba)
        dados = await run_in_threadpool(serpapi.pesquisar, "instagram_profile", profile_id=usuario)
        perfil, posts = serpapi.normalizar_perfil_instagram(dados)
        if not perfil.get("username"):
            return JSONResponse({"ok": False, "erro": "Perfil não encontrado ou privado."}, status_code=400)
        return JSONResponse({
            "ok": True,
            "perfil": {
                "username": perfil["username"],
                "nome": perfil["nome"],
                "biografia": perfil["biografia"],
                "seguidores": perfil["seguidores"],
                "seguindo": perfil["seguindo"],
                "privado": perfil["privado"],
                "verificado": perfil["verificado"],
                "link": perfil["link"],
                "foto_perfil": perfil.get("foto_perfil", ""),
            }
        })
    except (ValueError, serpapi.SerpApiNaoConfigurada, serpapi.ErroSerpApi) as erro:
        logger.info(f"DEBUG: SerpApi error - returning JSON: {erro}")
        return JSONResponse({"ok": False, "erro": str(erro)}, status_code=400)
    except Exception as erro:
        logger.exception("Erro inesperado ao verificar Instagram")
        return JSONResponse({"ok": False, "erro": "Erro interno do servidor"}, status_code=500)


@app.post("/debug-instagram")
async def debug_instagram(request: Request, arroba: str = Form(...), csrf_token: str = Form(...)):
    """Endpoint de DEBUG para testar resposta JSON"""
    logger.info(f"DEBUG endpoint called: arroba={arroba}")
    return JSONResponse(
        {
            "debug": True,
            "arroba": arroba,
            "csrf_sent": bool(csrf_token),
            "csrf_in_session": bool(request.session.get("csrf_token")),
            "message": "Este endpoint retorna JSON puro"
        }
    )


@app.post("/test-instagram")
async def test_instagram(request: Request, arroba: str = Form(...)):
    """Test endpoint without CSRF for debugging"""
    logger.info(f"Test instagram: arroba={arroba}")
    try:
        usuario = serpapi.validar_usuario_instagram(arroba)
        dados = await run_in_threadpool(serpapi.pesquisar, "instagram_profile", profile_id=usuario)
        perfil, posts = serpapi.normalizar_perfil_instagram(dados)
        if not perfil.get("username"):
            return JSONResponse({"ok": False, "erro": "Perfil não encontrado ou privado."}, status_code=400)
        return JSONResponse({
            "ok": True,
            "perfil": {
                "username": perfil["username"],
                "nome": perfil["nome"],
                "biografia": perfil["biografia"],
                "seguidores": perfil["seguidores"],
                "seguindo": perfil["seguindo"],
                "privado": perfil["privado"],
                "verificado": perfil["verificado"],
                "link": perfil["link"],
                "foto_perfil": perfil.get("foto_perfil", ""),
            }
        })
    except (ValueError, serpapi.SerpApiNaoConfigurada, serpapi.ErroSerpApi) as erro:
        return JSONResponse({"ok": False, "erro": str(erro)}, status_code=400)
    except Exception as erro:
        logger.exception("Erro inesperado ao verificar Instagram")
        return JSONResponse({"ok": False, "erro": "Erro interno do servidor"}, status_code=500)


@app.post("/cadastro", response_class=HTMLResponse, dependencies=_csrf())
async def cadastrar(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    nome_publico: str = Form(...),
    arroba: str = Form(""),
    arroba_confirmado: str = Form(""),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
):
    # Usar o arroba confirmado se disponível, senão o do formulário direto
    arroba_final = arroba_confirmado or arroba
    try:
        user_id, username, email = _criar_conta(
            username, email, nome_publico, arroba_final, senha, confirmar_senha
        )
        confirmacao_email_obrigatoria = autenticacao.confirmacao_email_obrigatoria()
    except ValueError as erro:
        return templates.TemplateResponse(
            request,
            "cadastro.html",
            {
                "erro": str(erro),
                "mensagem": "",
                "confirmacao_email_obrigatoria": autenticacao.confirmacao_email_obrigatoria(),
                "username_valor": username,
                "email_valor": email,
                "nome_publico_valor": nome_publico,
                "arroba_valor": arroba_final,
            },
            status_code=400,
        )
    except db.INTEGRITY_ERRORS:
        return templates.TemplateResponse(
            request,
            "cadastro.html",
            {
                "erro": "Esse nome de usuário ou e-mail já está cadastrado.",
                "mensagem": "",
                "confirmacao_email_obrigatoria": autenticacao.confirmacao_email_obrigatoria(),
                "username_valor": username,
                "email_valor": email,
                "nome_publico_valor": nome_publico,
                "arroba_valor": arroba_final,
            },
            status_code=400,
        )

    if confirmacao_email_obrigatoria:
        try:
            await _enviar_email_token(
                user_id,
                email,
                username,
                "confirmar_email",
            )
        except autenticacao.ErroEmail:
            logger.exception("Falha ao enviar confirmação de e-mail para novo cadastro")
            return templates.TemplateResponse(
                request,
                "cadastro.html",
                {
                    "erro": (
                        "A conta foi criada, mas não foi possível enviar o e-mail. "
                        "Use a opção de reenviar confirmação."
                    ),
                    "mensagem": "",
                    "confirmacao_email_obrigatoria": True,
                },
                status_code=503,
            )

        return templates.TemplateResponse(
            request,
            "cadastro.html",
            {
                "erro": "",
                "mensagem": (
                    "Cadastro concluído. Sua conta já está ativa."
                    if not confirmacao_email_obrigatoria
                    else "Cadastro recebido. Enviamos um link para confirmar seu e-mail."
                ),
                "confirmacao_email_obrigatoria": confirmacao_email_obrigatoria,
            },
        )

    _nova_sessao(request, user_id)
    # Conta nova cai no onboarding: e ali que ela diz o que sera monitorado.
    return RedirectResponse("/onboarding", status_code=303)


def _criar_conta(username, email, nome_publico, arroba, senha, confirmar_senha):
    username = autenticacao.validar_usuario(username)
    email = autenticacao.validar_email(email)
    nome_publico = nome_publico.strip()
    if not nome_publico or len(nome_publico) > 80:
        raise ValueError("Informe seu nome público com até 80 caracteres.")
    arroba = serpapi.validar_usuario_instagram(arroba)
    autenticacao.validar_senha(senha)
    if senha != confirmar_senha:
        raise ValueError("As senhas não coincidem.")
    if username.casefold() == ADMIN_USERNAME.casefold():
        raise ValueError("Esse nome de usuário está reservado.")
    if autenticacao.confirmacao_email_obrigatoria() and not autenticacao.email_configurado():
        raise ValueError("O envio de e-mail ainda não está configurado.")
    user_id = db.criar_usuario(
        username, email, autenticacao.hash_senha(senha), nome_publico, arroba
    )
    if not autenticacao.confirmacao_email_obrigatoria():
        db.confirmar_email(user_id)
    return user_id, username, email


@app.get("/confirmar-email", response_class=HTMLResponse)
def confirmar_email_formulario(request: Request, token: str = "", erro: str = ""):
    return templates.TemplateResponse(
        request,
        "confirmar_email.html",
        {"token": token, "erro": erro, "confirmado": False},
    )


@app.post(
    "/confirmar-email",
    response_class=HTMLResponse,
    dependencies=_csrf(),
)
def confirmar_email(request: Request, token: str = Form(...)):
    user_id = db.consumir_token_auth(
        autenticacao.hash_token(token),
        "confirmar_email",
    )
    if user_id is None:
        return templates.TemplateResponse(
            request,
            "confirmar_email.html",
            {
                "token": "",
                "erro": "O link é inválido ou expirou. Solicite outro e-mail.",
                "confirmado": False,
            },
            status_code=400,
        )
    db.confirmar_email(user_id)
    return templates.TemplateResponse(
        request,
        "confirmar_email.html",
        {"token": "", "erro": "", "confirmado": True},
    )


@app.post("/reenviar-confirmacao", response_class=HTMLResponse, dependencies=_csrf())
async def reenviar_confirmacao(request: Request, email: str = Form(...)):
    mensagem = "Se houver uma conta pendente para esse endereço, enviaremos um novo link."
    if not autenticacao.confirmacao_email_obrigatoria():
        return templates.TemplateResponse(
            request,
            "cadastro.html",
            {
                "erro": "",
                "mensagem": "A confirmação de e-mail está desativada no momento.",
                "confirmacao_email_obrigatoria": False,
            },
        )
    usuario = db.obter_usuario_por_email(email.strip())
    if usuario and not usuario["email_verified"] and autenticacao.email_configurado():
        try:
            await _enviar_email_token(
                usuario["id"],
                usuario["email"],
                usuario["username"],
                "confirmar_email",
            )
        except autenticacao.ErroEmail:
            logger.exception("Falha ao reenviar confirmação de e-mail")
    return templates.TemplateResponse(
        request,
        "cadastro.html",
        {
            "erro": "",
            "mensagem": mensagem,
            "confirmacao_email_obrigatoria": True,
        },
    )


@app.get("/esqueci-senha", response_class=HTMLResponse)
def esqueci_senha_formulario(request: Request, mensagem: str = "", erro: str = ""):
    return templates.TemplateResponse(
        request,
        "esqueci_senha.html",
        {"erro": erro, "mensagem": mensagem},
    )


@app.post("/esqueci-senha", response_class=HTMLResponse, dependencies=_csrf())
async def solicitar_redefinicao(request: Request, email: str = Form(...)):
    usuario = db.obter_usuario_por_email(email.strip())
    if usuario and usuario["email_verified"] and autenticacao.email_configurado():
        try:
            await _enviar_email_token(
                usuario["id"],
                usuario["email"],
                usuario["username"],
                "redefinir_senha",
            )
        except autenticacao.ErroEmail:
            logger.exception("Falha ao enviar link de redefinição de senha")
    return templates.TemplateResponse(
        request,
        "esqueci_senha.html",
        {
            "erro": "",
            "mensagem": (
                "Se houver uma conta confirmada para esse endereço, "
                "enviamos instruções para redefinir a senha."
            ),
        },
    )


@app.get("/redefinir-senha", response_class=HTMLResponse)
def redefinir_senha_formulario(
    request: Request,
    token: str = "",
    erro: str = "",
):
    return templates.TemplateResponse(
        request,
        "redefinir_senha.html",
        {"token": token, "erro": erro},
    )


@app.post("/redefinir-senha", response_class=HTMLResponse, dependencies=_csrf())
def redefinir_senha(
    request: Request,
    token: str = Form(...),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
):
    try:
        autenticacao.validar_senha(senha)
        if senha != confirmar_senha:
            raise ValueError("As senhas não coincidem.")
    except ValueError as erro:
        return templates.TemplateResponse(
            request,
            "redefinir_senha.html",
            {"token": token, "erro": str(erro)},
            status_code=400,
        )
    user_id = db.consumir_token_auth(
        autenticacao.hash_token(token),
        "redefinir_senha",
    )
    if user_id is None:
        return templates.TemplateResponse(
            request,
            "redefinir_senha.html",
            {
                "token": "",
                "erro": "O link é inválido ou expirou. Solicite uma nova redefinição.",
            },
            status_code=400,
        )
    db.atualizar_senha(user_id, autenticacao.hash_senha(senha))
    db.revogar_sessoes_usuario(user_id)
    return RedirectResponse("/login?mensagem=senha_alterada", status_code=303)


@app.post("/perfil-busca/alterar-senha", dependencies=_csrf())
def alterar_senha_perfil_busca(
    request: Request,
    senha_atual: str = Form(...),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
):
    usuario = db.obter_usuario_por_login(request.state.user["username"])
    if not usuario or not autenticacao.verificar_senha(
        usuario["password_hash"], senha_atual
    ):
        return RedirectResponse(
            "/perfil-busca?erro=" + quote("A senha atual está incorreta."),
            status_code=303,
        )
    try:
        autenticacao.validar_senha(senha)
        if senha != confirmar_senha:
            raise ValueError("As senhas não coincidem.")
    except ValueError as erro:
        return RedirectResponse(
            "/perfil-busca?erro=" + quote(str(erro)),
            status_code=303,
        )
    user_id = request.state.user["id"]
    db.atualizar_senha(user_id, autenticacao.hash_senha(senha))
    db.revogar_sessoes_usuario(user_id)
    _nova_sessao(request, user_id)
    return RedirectResponse("/", status_code=303)


@app.post("/logout", dependencies=_csrf())
def logout(request: Request):
    token = request.session.get("auth_session")
    if isinstance(token, str):
        db.revogar_sessao(autenticacao.hash_token(token))
    request.session.clear()
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


async def _ler_imagem(arquivo: UploadFile) -> bytes:
    dados = await arquivo.read(TAMANHO_MAXIMO + 1)
    if len(dados) > TAMANHO_MAXIMO:
        raise ValueError("O arquivo é maior que 20 MB.")
    return dados


async def _salvar_foto_busca(user_id: int, arquivo: UploadFile):
    extensao = Path(arquivo.filename or "").suffix.lower()
    if extensao not in EXTENSOES_PERMITIDAS:
        raise ValueError("Formato não suportado. Use JPG, PNG ou WEBP.")
    dados = await _ler_imagem(arquivo)
    imagens.calcular_phash(dados)
    nome_arquivo = f"{uuid.uuid4().hex}{extensao}"
    caminho = PASTA_FOTOS_BUSCA / nome_arquivo
    caminho.write_bytes(dados)
    try:
        foto_id = db.adicionar_foto_busca(
            user_id,
            nome_arquivo,
            arquivo.filename or nome_arquivo,
        )
    except db.INTEGRITY_ERRORS:
        caminho.unlink(missing_ok=True)
        raise
    return foto_id, nome_arquivo, dados


def _voltar_com_erro(mensagem: str):
    return RedirectResponse("/catalogo?erro=" + quote(mensagem), status_code=303)


def _voltar_ao_formulario_edicao(criadora_id: int, mensagem: str):
    return RedirectResponse(
        "/criadoras/{}/editar?erro=".format(criadora_id) + quote(mensagem),
        status_code=303,
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    contexto = {"titulo": "Monitoramento de Vazamentos"}
    if request.state.user and request.state.user["role"] == "admin":
        contexto["pendencias"] = _pendencias_de_operacao()
    return templates.TemplateResponse(request, "home.html", contexto)


@app.get(
    "/dev/componentes",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def componentes_dev(request: Request):
    """Vitrine do design system, apenas em desenvolvimento."""
    if not COMPONENTES_DEV_ATIVO:
        raise HTTPException(status_code=404, detail="Not Found")
    return templates.TemplateResponse(
        request, "dev_componentes.html", {"titulo": "Componentes (dev)"}
    )


def _pendencias_de_operacao():
    """Checklist do admin: contas que ainda nao conseguem gerar resultados."""
    pendencias = []
    for usuario in db.listar_usuarios():
        nome = usuario["public_name"] or usuario["username"]
        link = f"/usuarios/{usuario['id']}/editar"
        if not (usuario["public_name"] and usuario["public_handle"]):
            pendencias.append(
                {
                    "usuario": nome,
                    "texto": "perfil de busca incompleto (nome público e arroba)",
                    "link": link,
                    "acao": "Completar perfil",
                }
            )
            continue
        if not db.contar_candidatos_por_status(usuario["id"]):
            pendencias.append(
                {
                    "usuario": nome,
                    "texto": "nenhuma busca executada para esta conta",
                    "link": f"/busca-web?target_user_id={usuario['id']}",
                    "acao": "Rodar primeira busca",
                }
            )
        estado = _status_vazamento(usuario["id"])
        if estado.get("estado") == "erro":
            pendencias.append(
                {
                    "usuario": nome,
                    "texto": "a última busca falhou: "
                    + (estado.get("erro") or "erro não informado")[:120],
                    "link": f"/busca-web?target_user_id={usuario['id']}",
                    "acao": "Ver busca",
                }
            )
    return pendencias


@app.get("/saude")
def saude():
    return {"status": "ok"}


@app.post(
    "/usuarios/{usuario_id}/perfil",
    dependencies=[Depends(exigir_admin), *_csrf()],
)
def atualizar_perfil_usuario(
    request: Request,
    usuario_id: int,
    role: str = Form(...),
):
    if role not in {"admin", "cliente"}:
        return RedirectResponse(
            "/usuarios?erro=" + quote("Perfil de usuário inválido."),
            status_code=303,
        )
    try:
        atualizado = db.atualizar_perfil_usuario(usuario_id, role)
    except ValueError as erro:
        return RedirectResponse("/usuarios?erro=" + quote(str(erro)), status_code=303)
    if not atualizado:
        return RedirectResponse(
            "/usuarios?erro=" + quote("Usuário não encontrado."),
            status_code=303,
        )
    return RedirectResponse(
        ("/" if usuario_id == request.state.user["id"] and role != "admin"
         else "/usuarios?mensagem=" + quote("Perfil atualizado.")),
        status_code=303,
    )


@app.get("/perfil-busca", response_class=HTMLResponse)
def perfil_busca(request: Request, erro: str = "", mensagem: str = ""):
    usuario = db.obter_usuario_por_id(request.state.user["id"])
    return templates.TemplateResponse(
        request,
        "perfil_busca.html",
        {
            "usuario": usuario,
            "fotos": db.listar_fotos_busca(usuario["id"]),
            "perfis": db.listar_perfis_monitorados(usuario["id"]),
            "agendamento": agendamento.estado(usuario["id"]),
            "erro": erro,
            "mensagem": mensagem,
        },
    )


@app.post("/perfil-busca", dependencies=_csrf())
def salvar_perfil_busca(
    request: Request,
    nome_publico: str = Form(""),
    arroba: str = Form(...),
):
    nome_publico = nome_publico.strip()
    if len(nome_publico) > 80:
        return RedirectResponse(
            "/perfil-busca?erro="
            + quote("Informe um nome público com até 80 caracteres."),
            status_code=303,
        )
    try:
        arroba_normalizada = serpapi.validar_usuario_instagram(arroba)
    except ValueError as erro:
        return RedirectResponse(
            "/perfil-busca?erro=" + quote(str(erro)),
            status_code=303,
        )

    # O @ e obrigatorio e conferido no proprio Instagram: grafia errada deixa
    # de virar uma busca inteira sem resultado. A bio tambem alimenta os
    # arrobas descobertos e as paginas oficiais automaticas.
    aviso = ""
    try:
        dados = instagram.buscar_perfil(arroba_normalizada)
    except instagram.PerfilInstagramNaoEncontrado as erro:
        return RedirectResponse(
            "/perfil-busca?erro=" + quote(str(erro)),
            status_code=303,
        )
    except instagram.BuscaInstagramIndisponivel as erro:
        aviso = f" Perfil salvo, mas a validacao no Instagram falhou agora: {erro}"
    else:
        perfil = dados["perfil"]
        if not nome_publico:
            nome_publico = (perfil.get("nome") or "").strip()[:80]
        fluxo_busca.registrar_perfil_instagram(
            request.state.user["id"], arroba_normalizada, perfil
        )
    if not nome_publico:
        nome_publico = arroba_normalizada
    db.atualizar_perfil_busca(
        request.state.user["id"], nome_publico, arroba_normalizada
    )
    return RedirectResponse(
        "/perfil-busca?mensagem="
        + quote("Perfil de busca validado e salvo." + aviso),
        status_code=303,
    )


@app.post("/perfil-busca/fotos", dependencies=_csrf())
async def enviar_foto_busca(
    request: Request,
    arquivo: UploadFile = File(...),
):
    try:
        await _salvar_foto_busca(request.state.user["id"], arquivo)
    except ValueError as erro:
        return RedirectResponse(
            "/perfil-busca?erro=" + quote(str(erro)),
            status_code=303,
        )
    return RedirectResponse(
        "/perfil-busca?mensagem=" + quote("Foto salva para suas buscas."),
        status_code=303,
    )


@app.get("/fotos-busca/{foto_id}")
def obter_foto_busca(request: Request, foto_id: int):
    foto = db.obter_foto_busca(foto_id)
    if foto is None:
        raise HTTPException(status_code=404, detail="Foto não encontrada.")
    if (
        request.state.user["role"] != "admin"
        and foto["user_id"] != request.state.user["id"]
    ):
        raise HTTPException(status_code=403, detail="Acesso negado.")
    nome_arquivo = foto["arquivo"]
    if Path(nome_arquivo).name != nome_arquivo:
        raise HTTPException(status_code=404, detail="Foto não encontrada.")
    caminho = PASTA_FOTOS_BUSCA / nome_arquivo
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Arquivo de foto não encontrado.")
    return FileResponse(caminho, media_type=mimetypes.guess_type(nome_arquivo)[0])


@app.post("/perfil-busca/fotos/{foto_id}/excluir", dependencies=_csrf())
def excluir_foto_busca(request: Request, foto_id: int):
    foto = db.obter_foto_busca(foto_id)
    if foto is None or foto["user_id"] != request.state.user["id"]:
        return RedirectResponse(
            "/perfil-busca?erro=" + quote("Foto não encontrada."),
            status_code=303,
        )
    if db.excluir_foto_busca(foto_id, request.state.user["id"]):
        caminho = PASTA_FOTOS_BUSCA / foto["arquivo"]
        caminho.unlink(missing_ok=True)
    return RedirectResponse(
        "/perfil-busca?mensagem=" + quote("Foto removida."),
        status_code=303,
    )


# ------------------------------------------------------- perfis monitorados

def _destino_do_formulario(voltar_para: str) -> str:
    return "/configuracoes/perfis" if voltar_para == "config" else "/onboarding"


def _redirecionar_com(destino: str, passo: int, chave: str, texto: str):
    separador = "&" if "?" in destino else "?"
    return RedirectResponse(
        f"{destino}{separador}passo={passo}&{chave}={quote(texto)}", status_code=303
    )


def _contexto_perfis(request: Request, erro: str = "", mensagem: str = "") -> dict:
    criadora_id = request.state.user["id"]
    dados = perfis_core.estado(criadora_id)
    textos = {variante["texto"] for variante in dados["variantes"]}
    return {
        "estado": dados,
        "plataformas": perfis_core.PLATAFORMAS,
        "sugestoes": [
            texto
            for texto in perfis_core.sugerir_variantes(criadora_id)
            if texto not in textos
        ],
        "limite_perfis": db.limite_perfis_monitorados(),
        "agendamento": agendamento.estado(criadora_id),
        "status_textos": agendamento.STATUS_TEXTOS,
        "motivos": agendamento.MOTIVOS_FALHA,
        "erro": erro,
        "mensagem": mensagem,
    }


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, passo: int = 0, erro: str = "", mensagem: str = ""):
    contexto = _contexto_perfis(request, erro, mensagem)
    contexto["passo"] = passo if passo in (1, 2, 3, 4) else contexto["estado"]["atual"]
    return templates.TemplateResponse(request, "onboarding.html", contexto)


@app.get("/configuracoes/perfis", response_class=HTMLResponse)
def configuracoes_perfis(request: Request, erro: str = "", mensagem: str = ""):
    contexto = _contexto_perfis(request, erro, mensagem)
    contexto["passo"] = 1
    return templates.TemplateResponse(request, "perfis_monitorados.html", contexto)


def _voltar_para_perfis(chave: str, texto: str):
    return RedirectResponse(
        f"/configuracoes/perfis?{chave}={quote(texto)}", status_code=303
    )


@app.post("/configuracoes/agendamento", dependencies=_csrf())
def salvar_agendamento_da_criadora(
    request: Request,
    frequencia: str = Form("semanal"),
    hora: str = Form("9"),
    dia_semana: str = Form("0"),
):
    """Agenda diaria ou semanal da propria criadora (padrao: semanal)."""
    try:
        agendamento.salvar(request.state.user["id"], frequencia, hora, dia_semana)
    except ValueError as falha:
        return _voltar_para_perfis("erro", str(falha))
    return _voltar_para_perfis("mensagem", "Agendamento salvo.")


@app.post("/configuracoes/agendamento/desligar", dependencies=_csrf())
def desligar_agendamento_da_criadora(request: Request):
    if not agendamento.desativar(request.state.user["id"]):
        return _voltar_para_perfis("mensagem", "Não havia agendamento para desligar.")
    return _voltar_para_perfis("mensagem", "Agendamento desligado.")


@app.post("/configuracoes/escanear-agora", dependencies=_csrf())
def escanear_agora(request: Request):
    """Coloca uma busca na fila sem esperar o horario agendado."""
    execucao_id, mensagem = agendamento.disparar_agora(
        request.state.user["id"], origem="manual"
    )
    return _voltar_para_perfis("mensagem" if execucao_id else "erro", mensagem)


@app.get("/configuracoes/historico", response_class=HTMLResponse)
def historico_de_execucoes(request: Request):
    """Historico da propria criadora: data, provedor, buscas, novos, duracao."""
    return templates.TemplateResponse(
        request,
        "historico.html",
        {
            "execucoes": db.listar_execucoes_criadora(request.state.user["id"]),
            "motivos": agendamento.MOTIVOS_FALHA,
            "status_textos": agendamento.STATUS_TEXTOS,
            "provedores": busca_provedores.PROVEDORES_NOMES,
        },
    )


@app.post("/onboarding/perfis", dependencies=_csrf())
def salvar_perfis_monitorados(
    request: Request,
    plataforma: list[str] = Form([]),
    arroba: list[str] = Form([]),
    voltar_para: str = Form(""),
):
    criadora_id = request.state.user["id"]
    destino = _destino_do_formulario(voltar_para)
    adicionados = 0
    for indice, valor in enumerate(arroba):
        if not (valor or "").strip():
            continue
        escolhida = plataforma[indice] if indice < len(plataforma) else "instagram"
        try:
            if perfis_core.salvar_perfil(criadora_id, escolhida, valor):
                adicionados += 1
        except ValueError as falha:
            return _redirecionar_com(destino, 1, "erro", str(falha))
    if not adicionados:
        return _redirecionar_com(
            destino, 1, "mensagem", "Nenhum perfil novo para salvar."
        )
    return _redirecionar_com(
        destino, 1, "mensagem", f"{adicionados} perfil(is) salvo(s)."
    )


@app.post("/onboarding/perfis/{perfil_id}/status", dependencies=_csrf())
def alternar_perfil_monitorado(
    request: Request, perfil_id: int, voltar_para: str = Form("")
):
    criadora_id = request.state.user["id"]
    destino = _destino_do_formulario(voltar_para)
    perfil = db.obter_perfil_monitorado(perfil_id, criadora_id)
    if perfil is None:
        return _redirecionar_com(destino, 1, "erro", "Perfil não encontrado.")
    novo = "pausado" if perfil["status"] == "ativo" else "ativo"
    db.atualizar_status_perfil_monitorado(perfil_id, criadora_id, novo)
    return _redirecionar_com(destino, 1, "mensagem", f"Perfil {novo}.")


@app.post("/onboarding/perfis/{perfil_id}/excluir", dependencies=_csrf())
def excluir_perfil_monitorado(
    request: Request, perfil_id: int, voltar_para: str = Form("")
):
    destino = _destino_do_formulario(voltar_para)
    if not db.excluir_perfil_monitorado(perfil_id, request.state.user["id"]):
        return _redirecionar_com(destino, 1, "erro", "Perfil não encontrado.")
    return _redirecionar_com(destino, 1, "mensagem", "Perfil removido.")


@app.post("/onboarding/variantes", dependencies=_csrf())
def salvar_variantes_do_onboarding(
    request: Request,
    nova_variante: list[str] = Form([]),
    texto_novo: str = Form(""),
    variante_ativa: list[int] = Form([]),
    voltar_para: str = Form(""),
):
    criadora_id = request.state.user["id"]
    destino = _destino_do_formulario(voltar_para)
    existentes = {linha["id"] for linha in db.listar_variantes_nome(criadora_id)}
    criadas = perfis_core.salvar_variantes(criadora_id, nova_variante)
    if texto_novo.strip():
        criadas += perfis_core.salvar_variantes(
            criadora_id, [texto_novo], origem="manual"
        )
    ativas = {int(item) for item in variante_ativa}
    for variante in db.listar_variantes_nome(criadora_id):
        if variante["id"] in existentes:
            db.atualizar_variante_nome(
                variante["id"], criadora_id, variante["id"] in ativas
            )
    perfis_core.marcar_variantes_vistas(criadora_id)
    mensagem = (
        f"{criadas} variação(ões) nova(s) e lista atualizada."
        if criadas
        else "Lista de variações atualizada."
    )
    return _redirecionar_com(destino, 2, "mensagem", mensagem)


@app.post("/onboarding/variantes/{variante_id}/excluir", dependencies=_csrf())
def excluir_variante_do_onboarding(
    request: Request, variante_id: int, voltar_para: str = Form("")
):
    destino = _destino_do_formulario(voltar_para)
    if not db.excluir_variante_nome(variante_id, request.state.user["id"]):
        return _redirecionar_com(destino, 2, "erro", "Variação não encontrada.")
    return _redirecionar_com(destino, 2, "mensagem", "Variação removida.")


@app.post("/onboarding/oficiais", dependencies=_csrf())
def salvar_pagina_oficial_do_onboarding(
    request: Request, url: str = Form(""), voltar_para: str = Form("")
):
    destino = _destino_do_formulario(voltar_para)
    try:
        criada = perfis_core.salvar_pagina(request.state.user["id"], url)
    except ValueError as falha:
        return _redirecionar_com(destino, 3, "erro", str(falha))
    if not criada:
        return _redirecionar_com(
            destino, 3, "mensagem", "Essa página já estava na lista."
        )
    return _redirecionar_com(destino, 3, "mensagem", "Página oficial adicionada.")


@app.post("/onboarding/oficiais/pular", dependencies=_csrf())
def pular_paginas_oficiais(request: Request, voltar_para: str = Form("")):
    perfis_core.pular_oficiais(request.state.user["id"])
    destino = _destino_do_formulario(voltar_para)
    return _redirecionar_com(
        destino, 4, "mensagem", "Etapa de páginas oficiais marcada como pulada."
    )


@app.post("/onboarding/oficiais/{pagina_id}/excluir", dependencies=_csrf())
def excluir_pagina_oficial_do_onboarding(
    request: Request, pagina_id: int, voltar_para: str = Form("")
):
    destino = _destino_do_formulario(voltar_para)
    if not db.excluir_pagina_oficial(pagina_id, request.state.user["id"]):
        return _redirecionar_com(destino, 3, "erro", "Página não encontrada.")
    return _redirecionar_com(destino, 3, "mensagem", "Página removida.")


@app.post("/onboarding/concluir", dependencies=_csrf())
def concluir_onboarding(request: Request):
    perfis_core.concluir(request.state.user["id"])
    return RedirectResponse(
        "/onboarding?passo=4&mensagem="
        + quote("Configuração salva. Você pode ajustar isso depois em Configurações > Perfis."),
        status_code=303,
    )


@app.post("/onboarding/busca", dependencies=_csrf())
def primeira_busca_do_onboarding(request: Request, tarefas: BackgroundTasks):
    """Dispara o motor atual uma vez, pelo botão. Sem agendamento."""
    criadora_id = request.state.user["id"]
    alvo = db.obter_usuario_por_id(criadora_id)
    destino = "/onboarding"
    arrobas = perfis_core.arrobas_monitoradas(criadora_id)
    if not arrobas and not (alvo["public_handle"] or "").strip():
        return _redirecionar_com(
            destino, 4, "erro", "Adicione ao menos um perfil antes de buscar."
        )
    provedor = next(
        (
            nome
            for nome in ("serpapi", "tinyfish")
            if busca_provedores.configurado(nome)
        ),
        "",
    )
    if not provedor:
        return _redirecionar_com(
            destino,
            4,
            "erro",
            "Configure SERPAPI_API_KEY ou TINYFISH_API_KEY no servidor para rodar a busca.",
        )
    atual = _status_vazamento(criadora_id)
    if atual.get("estado") == "rodando" and (
        time.time() - atual.get("iniciado", 0) < TEMPO_MAXIMO_BUSCA_VAZAMENTO
    ):
        return _redirecionar_com(
            destino, 4, "erro", "Já existe uma busca em andamento para esta conta."
        )
    termos, sites = _config_vazamentos()
    limite = _limite_consultas_busca()
    consultas = consultas_vazamento.gerar_consultas_busca(
        arrobas or [alvo["public_handle"]],
        termos,
        sites,
        limite,
        nome=alvo["public_name"] or "",
    )
    if not consultas:
        return _redirecionar_com(
            destino, 4, "erro", "Cadastre ao menos um termo ou site para pesquisar."
        )
    _salvar_status_vazamento(
        criadora_id,
        {
            "estado": "rodando",
            "iniciado": time.time(),
            "consultas": consultas,
            "api": "google_images_light",
            "provedor": provedor,
        },
    )
    tarefas.add_task(
        _executar_busca_provedor,
        alvo,
        provedor,
        termos,
        sites,
        limite,
        "google_images_light",
        {"gl": "br", "hl": "pt", "safe": "off"},
    )
    perfis_core.concluir(criadora_id)
    return _redirecionar_com(
        destino,
        4,
        "mensagem",
        "Primeira busca iniciada em segundo plano. Os candidatos aparecem na Revisão.",
    )


@app.get(
    "/catalogo",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
@app.get(
    "/usuarios",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def catalogo(request: Request, erro: str = "", mensagem: str = ""):
    contexto = {
        "usuarios": db.listar_usuarios(),
        "erro": erro,
        "mensagem": mensagem,
    }
    return templates.TemplateResponse(request, "catalogo.html", contexto)


@app.get("/usuarios/novo", response_class=HTMLResponse, dependencies=[Depends(exigir_admin)])
def novo_usuario_formulario(request: Request, erro: str = ""):
    return templates.TemplateResponse(
        request, "usuario_form.html", {"erro": erro}
    )


@app.post("/usuarios", response_class=HTMLResponse, dependencies=_admin_csrf())
async def novo_usuario(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    nome_publico: str = Form(...),
    arroba: str = Form(...),
    arroba_confirmado: str = Form(""),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
):
    # Usar o arroba confirmado via verificação do Instagram, se disponível
    arroba_final = arroba_confirmado or arroba
    try:
        user_id, username, email = _criar_conta(
            username, email, nome_publico, arroba_final, senha, confirmar_senha
        )
    except ValueError as erro:
        return templates.TemplateResponse(
            request, "usuario_form.html", {"erro": str(erro)}, status_code=400
        )
    except db.INTEGRITY_ERRORS:
        return templates.TemplateResponse(
            request, "usuario_form.html",
            {"erro": "Esse nome de usuário ou e-mail já está cadastrado."},
            status_code=400,
        )
    if autenticacao.confirmacao_email_obrigatoria():
        try:
            await _enviar_email_token(user_id, email, username, "confirmar_email")
        except autenticacao.ErroEmail:
            logger.exception("Falha ao enviar confirmação de e-mail para novo usuário")
            return RedirectResponse(
                "/usuarios?erro=" + quote(
                    "Conta criada, mas o e-mail de confirmação não foi enviado. "
                    "Use a opção de reenviar confirmação no cadastro."
                ),
                status_code=303,
            )
        mensagem = "Usuário criado. Enviamos um link para confirmar o e-mail."
    else:
        mensagem = "Usuário criado. Informe a senha inicial por um meio seguro."
    return RedirectResponse("/usuarios?mensagem=" + quote(mensagem), status_code=303)


@app.get("/usuarios/{usuario_id}/editar", response_class=HTMLResponse,
         dependencies=[Depends(exigir_admin)])
def editar_usuario_formulario(request: Request, usuario_id: int, erro: str = ""):
    usuario = db.obter_usuario_por_id(usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    return templates.TemplateResponse(
        request, "usuario_editar.html", {"usuario": usuario, "erro": erro}
    )


@app.post("/usuarios/{usuario_id}/editar", response_class=HTMLResponse,
          dependencies=_admin_csrf())
async def editar_usuario(
    request: Request,
    usuario_id: int,
    username: str = Form(...),
    email: str = Form(...),
    nome_publico: str = Form(...),
    arroba: str = Form(...),
    role: str = Form("cliente"),
    senha: str = Form(""),
    confirmar_senha: str = Form(""),
):
    usuario = db.obter_usuario_por_id(usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    try:
        username = autenticacao.validar_usuario(username)
        if (username.casefold() == ADMIN_USERNAME.casefold()
                and username.casefold() != usuario["username"].casefold()):
            raise ValueError("Esse nome de usuário está reservado.")
        email = autenticacao.validar_email(email)
        nome_publico = nome_publico.strip()
        if not nome_publico or len(nome_publico) > 80:
            raise ValueError("Informe o nome público com até 80 caracteres.")
        arroba = serpapi.validar_usuario_instagram(arroba)
        if senha != confirmar_senha:
            raise ValueError("As senhas não coincidem.")
        password_hash = autenticacao.hash_senha(senha) if senha else None
        if (email != usuario["email"] and autenticacao.confirmacao_email_obrigatoria()
                and not autenticacao.email_configurado()):
            raise ValueError("Configure o envio de e-mail antes de alterar o endereço.")
        email_alterado = db.editar_usuario(
            usuario_id, username, email, nome_publico, arroba, role, password_hash
        )
        if email_alterado is None:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    except ValueError as erro:
        return templates.TemplateResponse(
            request, "usuario_editar.html",
            {"usuario": {**dict(usuario), "username": username, "email": email,
                         "public_name": nome_publico, "public_handle": arroba,
                         "role": role}, "erro": str(erro)}, status_code=400,
        )
    except db.INTEGRITY_ERRORS:
        return templates.TemplateResponse(
            request, "usuario_editar.html",
            {"usuario": usuario, "erro": "Esse nome de usuário ou e-mail já está cadastrado."},
            status_code=400,
        )
    mensagem = "Usuário atualizado."
    falha_email = False
    if email_alterado and autenticacao.confirmacao_email_obrigatoria():
        try:
            await _enviar_email_token(usuario_id, email, username, "confirmar_email")
        except autenticacao.ErroEmail:
            logger.exception("Falha ao enviar confirmação de e-mail alterado")
            falha_email = True
            mensagem = ("Usuário atualizado, mas o e-mail de confirmação não foi enviado. "
                        "Solicite o reenvio na página de cadastro.")
        else:
            mensagem = "Usuário atualizado. Confirme o novo e-mail antes de entrar."
    if usuario_id == request.state.user["id"] and (email_alterado or password_hash):
        request.session.pop("auth_session", None)
        return RedirectResponse(
            "/login?" + ("erro=" if falha_email else "mensagem=") + quote(mensagem),
            status_code=303,
        )
    if usuario_id == request.state.user["id"] and role != "admin":
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(
        "/usuarios?" + ("erro=" if falha_email else "mensagem=") + quote(mensagem),
        status_code=303,
    )


@app.post("/usuarios/{usuario_id}/excluir", dependencies=_admin_csrf())
def excluir_usuario(request: Request, usuario_id: int):
    try:
        arquivos = db.excluir_usuario(usuario_id)
    except ValueError as erro:
        return RedirectResponse("/usuarios?erro=" + quote(str(erro)), status_code=303)
    if arquivos is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    falha = False
    for pasta, nomes in ((PASTA_FOTOS_BUSCA, arquivos[0]), (PASTA_OFICIAIS, arquivos[1])):
        for nome in nomes:
            if Path(nome).name != nome:
                logger.error("Arquivo de usuário com caminho inválido: %s", nome)
                falha = True
                continue
            try:
                (pasta / nome).unlink(missing_ok=True)
            except OSError:
                logger.exception("Falha ao remover arquivo de usuário: %s", nome)
                falha = True
    mensagem = ("Usuário excluído, mas alguns arquivos não puderam ser removidos; consulte os logs."
                if falha else "Usuário excluído.")
    if usuario_id == request.state.user["id"]:
        request.session.clear()
        return RedirectResponse(
            "/login?" + ("erro=" if falha else "mensagem=") + quote(mensagem),
            status_code=303,
        )
    return RedirectResponse(
        "/usuarios?" + ("erro=" if falha else "mensagem=") + quote(mensagem),
        status_code=303,
    )


@app.post("/sites-oficiais", dependencies=_admin_csrf())
def novo_site_oficial(
    criadora_id: int = Form(...),
    nome: str = Form(...),
    url: str = Form(...),
):
    nome = nome.strip()
    if not nome or len(nome) > 80:
        return _voltar_com_erro("O nome do site deve ter entre 1 e 80 caracteres.")
    try:
        url = sites_oficiais.normalizar_url_pagina(url)
    except ValueError as erro:
        return _voltar_com_erro(str(erro))
    try:
        db.adicionar_site_oficial(criadora_id, nome, url)
    except db.INTEGRITY_ERRORS:
        return _voltar_com_erro(
            "Essa URL já está cadastrada para a criadora selecionada."
        )
    return RedirectResponse(
        "/catalogo?mensagem=" + quote("Página oficial cadastrada."),
        status_code=303,
    )


@app.post("/sites-oficiais/{site_id}/editar", dependencies=_admin_csrf())
def editar_site_oficial(
    site_id: int,
    criadora_id: int = Form(...),
    nome: str = Form(...),
    url: str = Form(...),
):
    nome = nome.strip()
    if not nome or len(nome) > 80:
        return _voltar_com_erro("O nome da página deve ter entre 1 e 80 caracteres.")
    try:
        url = sites_oficiais.normalizar_url_pagina(url)
    except ValueError as erro:
        return _voltar_com_erro(str(erro))
    try:
        atualizado = db.atualizar_site_oficial(
            site_id, criadora_id, nome, url
        )
    except db.INTEGRITY_ERRORS:
        return _voltar_com_erro(
            "Essa URL já está cadastrada para a criadora selecionada."
        )
    if not atualizado:
        return _voltar_com_erro("Página oficial não encontrada.")
    return RedirectResponse(
        "/catalogo?mensagem=" + quote("Página oficial atualizada."),
        status_code=303,
    )


@app.post("/sites-oficiais/{site_id}/excluir", dependencies=_admin_csrf())
def excluir_site_oficial(site_id: int):
    if not db.excluir_site_oficial(site_id):
        return _voltar_com_erro("Página oficial não encontrada.")
    return RedirectResponse(
        "/catalogo?mensagem=" + quote("Página oficial removida."),
        status_code=303,
    )


@app.post("/criadoras/{criadora_id}/editar", dependencies=_admin_csrf())
def editar_criadora(
    criadora_id: int,
    nome: str = Form(...),
    email: str = Form(""),
    arroba: str = Form(""),
):
    nome = nome.strip()
    if not nome or len(nome) > 80:
        return _voltar_ao_formulario_edicao(
            criadora_id, "O nome da criadora deve ter entre 1 e 80 caracteres."
        )
    email = email.strip()
    if email:
        try:
            email = autenticacao.validar_email(email)
        except ValueError as erro:
            return _voltar_ao_formulario_edicao(criadora_id, str(erro))
    handle = ""
    if arroba.strip():
        try:
            handle = serpapi.validar_usuario_instagram(arroba)
        except ValueError as erro:
            return _voltar_ao_formulario_edicao(criadora_id, str(erro))
    try:
        atualizado = db.atualizar_criadora_contato(
            criadora_id, nome, email, handle
        )
    except db.INTEGRITY_ERRORS:
        return _voltar_ao_formulario_edicao(
            criadora_id, "Já existe um perfil de criadora com esse nome ou e-mail."
        )
    if not atualizado:
        return _voltar_ao_formulario_edicao(
            criadora_id, "Perfil não encontrado ou já associado a uma conta."
        )
    return RedirectResponse(
        "/usuarios?mensagem=" + quote("Perfil de criadora atualizado."),
        status_code=303,
    )


@app.get(
    "/criadoras/nova",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def nova_criadora_formulario(request: Request, erro: str = ""):
    return RedirectResponse("/usuarios/novo", status_code=303)


@app.get(
    "/criadoras/{criadora_id}/editar",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def editar_criadora_formulario(request: Request, criadora_id: int, erro: str = ""):
    criadora = db.obter_criadora(criadora_id)
    if criadora is None:
        return RedirectResponse(
            "/usuarios?erro=" + quote("Criadora não encontrada."), status_code=303
        )
    conteudos = db.listar_conteudos_criadora(criadora_id)
    sites = db.listar_sites_oficiais_criadora(criadora_id)
    criadora = dict(criadora)
    criadora["conteudos"] = conteudos
    criadora["sites_oficiais"] = sites
    return templates.TemplateResponse(
        request,
        "criadora_form.html",
        {"criadora": criadora, "erro": erro},
    )


@app.post("/criadoras/{criadora_id}/excluir", dependencies=_admin_csrf())
def excluir_criadora(criadora_id: int):
    try:
        arquivos = db.excluir_criadora(criadora_id)
    except ValueError:
        return _voltar_com_erro(
            "Criadora associada a uma conta não pode ser excluída."
        )
    if arquivos is None:
        return _voltar_com_erro("Criadora não encontrada.")
    for nome_arquivo in arquivos:
        try:
            (PASTA_OFICIAIS / nome_arquivo).unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "Criadora %s removida do banco, mas o arquivo %s não pôde ser excluído",
                criadora_id,
                nome_arquivo,
            )
    return RedirectResponse(
        "/usuarios?mensagem=" + quote("Criadora excluída com sucesso."),
        status_code=303,
    )


@app.post("/conteudos", dependencies=_admin_csrf())
async def novo_conteudo(criadora_id: int = Form(...), arquivo: UploadFile = File(...)):
    extensao = Path(arquivo.filename or "").suffix.lower()
    if extensao not in EXTENSOES_PERMITIDAS:
        return _voltar_com_erro("Formato não suportado. Use JPG, PNG ou WEBP.")
    try:
        dados = await _ler_imagem(arquivo)
        phash = imagens.calcular_phash(dados)
    except ValueError as erro:
        return _voltar_com_erro(str(erro))

    nome_arquivo = f"{uuid.uuid4().hex}{extensao}"
    (PASTA_OFICIAIS / nome_arquivo).write_bytes(dados)
    db.adicionar_conteudo(criadora_id, "foto", nome_arquivo, arquivo.filename, [phash])
    return RedirectResponse("/catalogo", status_code=303)


@app.post("/conteudos/{conteudo_id}/excluir", dependencies=_admin_csrf())
def excluir_conteudo(conteudo_id: int):
    nome_arquivo = db.excluir_conteudo(conteudo_id)
    if nome_arquivo is None:
        return _voltar_com_erro("Conteúdo não encontrado.")

    try:
        (PASTA_OFICIAIS / nome_arquivo).unlink(missing_ok=True)
    except OSError:
        logger.exception(
            "Conteúdo %s removido do banco, mas o arquivo %s não pôde ser excluído",
            conteudo_id,
            nome_arquivo,
        )
        return _voltar_com_erro(
            "Conteúdo removido do banco, mas o arquivo não pôde ser excluído."
        )

    mensagem = quote("Conteúdo excluído com sucesso.")
    return RedirectResponse("/catalogo?mensagem=" + mensagem, status_code=303)


def _referencias_do_usuario(alvo):
    """Hashes de referência: catálogo, fotos do perfil e sites oficiais."""
    nome = dict(alvo).get("public_name")
    referencias = [
        {"phash": linha["phash"], "origem": f"Catálogo — {nome or linha['criadora']}"}
        for linha in db.listar_hashes_criadora_do_usuario(alvo["id"])
    ]
    for foto in db.listar_fotos_busca(alvo["id"]):
        if Path(foto["arquivo"]).name != foto["arquivo"]:
            continue
        try:
            phash = imagens.calcular_phash(
                (PASTA_FOTOS_BUSCA / foto["arquivo"]).read_bytes()
            )
        except (OSError, ValueError):
            continue
        referencias.append(
            {"phash": phash, "origem": f"Foto do perfil — {foto['nome_original']}"}
        )
    referencias.extend(
        {"phash": linha["phash"], "origem": f"Site oficial — {linha['url']}"}
        for linha in db.listar_hashes_oficiais(alvo["id"])
        if linha["phash"]
    )
    return referencias


STATUS_VALIDA = {
    "pending": "Pendentes",
    "abaixo": "Abaixo do corte",
    "leaked": "Vazados",
    "not_me": "Não sou eu",
    "official": "Oficiais",
}

CLASSIFICACAO_NOMES = {
    "provavel_vazamento": "Provável vazamento",
    "oficial": "Oficial",
    "falso_positivo": "Falso positivo",
    "indefinido": "Indefinido",
}


def comparacao_imagem_ativa() -> bool:
    """Liga/desliga a comparacao por pHash sem remover o codigo existente."""
    return os.getenv("COMPARACAO_IMAGEM_ATIVA", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


BuscaInstagramIndisponivel = instagram.BuscaInstagramIndisponivel
USER_AGENT_IMAGEM = instagram.USER_AGENT_IMAGEM


def _baixar_miniatura(url: str) -> str:
    return instagram.baixar_hash_da_miniatura(url)


def _buscar_postagens_instagram(alvo: dict) -> list[dict]:
    """Busca postagens recentes e calcula o pHash das miniaturas (rota legada)."""
    dados_alvo = dict(alvo) if not isinstance(alvo, dict) else alvo
    _perfil, posts = instagram.buscar_postagens(
        dados_alvo.get("public_handle") or ""
    )
    return posts


@app.get("/valida", response_class=HTMLResponse)
def valida(
    request: Request,
    target_user_id: int = 0,
    status: str = "pending",
    mensagem: str = "",
):
    """Revisao dos candidatos ja coletados; nao consulta o Instagram."""
    alvo = _usuario_alvo_busca(request, target_user_id)
    if status not in STATUS_VALIDA:
        status = "pending"

    comparacao = comparacao_imagem_ativa()
    nota_minima = pontuacao.limiar_revisao()
    referencias = _referencias_do_usuario(alvo) if comparacao else []
    if comparacao:
        for post in db.listar_postagens_instagram(alvo["id"]):
            if post["phash"]:
                referencias.append(
                    {"phash": post["phash"], "origem": f"Instagram — {post['shortcode']}"}
                )

    # "Abaixo do corte" nao e um status no banco: e a mesma fila de pendentes
    # que a nota minima esconde. Mostrar essa lista evita a impressao de fila
    # vazia quando existe trabalho abaixo do limiar.
    if status == "abaixo":
        fila = (
            []
            if comparacao
            else [
                item
                for item in db.listar_candidatos_revisao(alvo["id"], "pending", None)
                if int(item["nota"] or 0) < nota_minima
            ]
        )
    else:
        fila = db.listar_candidatos_revisao(
            alvo["id"], status, None if comparacao else nota_minima
        )

    candidatos = []
    for candidato in fila:
        item = dict(candidato)
        motivos = item.get("motivos_json")
        if isinstance(motivos, (list, dict)):
            item["motivos"] = motivos if isinstance(motivos, list) else [motivos]
        else:
            try:
                item["motivos"] = json.loads(motivos or "[]")
            except (TypeError, ValueError):
                item["motivos"] = []
        item["novo"] = bool(item.get("novo_para_criadora"))
        if comparacao:
            melhor = candidatos_core.melhor_referencia(candidato["phash"], referencias)
            distancia = melhor["distancia"] if melhor else None
            listagem = sites_oficiais.pagina_de_listagem(candidato["url"])
            item.update(
                {
                    "distancia": distancia,
                    "pontuacao": (
                        imagens.pontuacao(distancia)
                        if distancia is not None
                        else None
                    ),
                    "referencia": melhor["origem"] if melhor else "",
                    "listagem": listagem,
                    "nivel": (
                        "diferente"
                        if listagem
                        else imagens.nivel_de_similaridade(distancia)
                    ),
                }
            )
        candidatos.append(item)

    # O mesmo album em sites diferentes (bunkr.cr/a/<id> e
    # bunkrsearch.com/albums/<id>) vira um item principal com duplicados.
    principais = []
    por_chave: dict[str, dict] = {}
    for item in candidatos:
        chave = item.get("chave_grupo") or ""
        item["duplicados"] = []
        if chave and chave in por_chave:
            por_chave[chave]["duplicados"].append(item)
            continue
        if chave:
            por_chave[chave] = item
        principais.append(item)
    candidatos = principais
    if comparacao:
        candidatos = candidatos_core.ordenar_por_atencao(candidatos)

    return templates.TemplateResponse(
        request,
        "valida.html",
        {
            "target_user": alvo,
            "target_user_id": alvo["id"],
            "target_users": (
                db.listar_usuarios_para_busca()
                if request.state.user["role"] == "admin"
                else []
            ),
            "candidatos": candidatos,
            "grupos": len(por_chave),
            "pontuacao_atencao": imagens.pontuacao(imagens.LIMITE_PROVAVEL),
            "pontuacao_possivel": imagens.pontuacao(imagens.LIMITE_POSSIVEL),
            "status_atual": status,
            "status_nomes": STATUS_VALIDA,
            "classificacao_nomes": CLASSIFICACAO_NOMES,
            "contagens": {
                **db.contar_candidatos_por_status(alvo["id"]),
                "abaixo": (
                    0 if comparacao
                    else db.contar_candidatos_abaixo_do_limiar(
                        alvo["id"], nota_minima
                    )
                ),
            },
            "tem_referencias": bool(referencias),
            "mensagem": mensagem,
            "comparacao_imagem_ativa": comparacao,
            "nota_minima": nota_minima,
        },
    )


@app.post("/valida/buscar-instagram", dependencies=_csrf())
async def valida_buscar_instagram_agora(request: Request):
    """Forca uma nova busca de postagens do Instagram para o usuario alvo."""
    # O formulario informa qual criadora esta selecionada na pagina; sem isso a
    # busca salvaria as postagens na conta do proprio admin logado.
    formulario = await request.form()
    try:
        alvo_id = int(formulario.get("target_user_id") or 0)
    except (TypeError, ValueError):
        alvo_id = 0
    alvo = _usuario_alvo_busca(request, alvo_id)
    try:
        posts = _buscar_postagens_instagram(alvo)
    except BuscaInstagramIndisponivel as erro:
        return JSONResponse({"ok": False, "erro": str(erro)}, status_code=400)
    except Exception as erro:
        logger.exception("Falha ao forcar busca de Instagram para valida")
        return JSONResponse(
            {"ok": False, "erro": f"Erro inesperado: {erro}"}, status_code=500
        )
    try:
        salvos = db.salvar_postagens_instagram(alvo["id"], posts) if posts else 0
        db.registrar_busca_instagram(alvo["id"])
        ultima = db.obter_ultima_busca_instagram(alvo["id"])
    except Exception as erro:
        logger.exception("Falha ao salvar postagens do Instagram para valida")
        return JSONResponse(
            {"ok": False, "erro": f"Erro ao salvar as postagens: {erro}"},
            status_code=500,
        )
    return JSONResponse(
        {
            "ok": True,
            "postagens": len(posts),
            "salvos": salvos,
            "ultima_busca": ultima,
        }
    )


@app.get("/analise", response_class=HTMLResponse, dependencies=[Depends(exigir_admin)])
def analise_dados(request: Request):
    por_usuario = {}
    for candidato in db.listar_todos_candidatos():
        por_usuario.setdefault(candidato["user_id"], []).append(candidato)
    vazados = db.contar_sites_vazados_por_usuario()
    relatorios = []
    for usuario in db.listar_usuarios_para_busca():
        lista = por_usuario.get(usuario["id"], [])
        if not lista and not vazados.get(usuario["id"]):
            continue
        referencias = _referencias_do_usuario(usuario)
        relatorio = analise.analisar_usuario(
            lista,
            referencias,
            sum(1 for h in db.listar_hashes_oficiais(usuario["id"]) if h["phash"]),
        )
        relatorios.append(
            {
                "usuario": usuario,
                "vazados_registrados": vazados.get(usuario["id"], 0),
                **relatorio,
            }
        )
    return templates.TemplateResponse(
        request,
        "analise.html",
        {
            "relatorios": relatorios,
            "status_nomes": STATUS_VALIDA,
            "pontuacao_atencao": imagens.pontuacao(imagens.LIMITE_PROVAVEL),
            "pontuacao_possivel": imagens.pontuacao(imagens.LIMITE_POSSIVEL),
        },
    )


@app.get(
    "/dominios",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def dominios_suspeitos_pagina(
    request: Request, status: str = "sugerido", mensagem: str = ""
):
    """Revisao humana dos dominios: aprovar (com tipo) ou rejeitar."""
    if status not in {"sugerido", "aprovado", "rejeitado", "todos"}:
        status = "sugerido"
    itens = db.listar_dominios_suspeitos(None if status == "todos" else status)
    itens.sort(
        key=lambda item: (
            -int(item.get("nota") or 0),
            -int(item["n_execucoes_distintas"] or 0),
            item["dominio"],
        )
    )
    return templates.TemplateResponse(
        request,
        "dominios.html",
        {
            "dominios": itens,
            "status_atual": status,
            "tipos": dominios_core.TIPOS,
            "status_opcoes": dominios_core.STATUS,
            "contagens": {
                **{
                    chave: len(db.listar_dominios_suspeitos(chave))
                    for chave in dominios_core.STATUS
                },
                "todos": len(db.listar_dominios_suspeitos(None)),
            },
            "limiar": dominios_core.limiar(),
            "minimo_execucoes": dominios_core.minimo_execucoes(),
            "minimo_criadoras": dominios_core.minimo_criadoras(),
            "mensagem": mensagem,
        },
    )


@app.post("/dominios/{dominio}/status", dependencies=_admin_csrf())
def atualizar_dominio_suspeito(
    request: Request,
    dominio: str,
    acao: str = Form(...),
    tipo: str = Form(""),
):
    if acao not in {"aprovar", "rejeitar"}:
        raise HTTPException(status_code=400, detail="Ação inválida.")
    if acao == "aprovar":
        # Aprovar exige o tipo escolhido pelo revisor; sem isso nada e gravado.
        if tipo not in dominios_core.TIPOS:
            raise HTTPException(status_code=400, detail="Tipo de domínio inválido.")
        atualizado = db.atualizar_status_dominio(
            dominio, "aprovado", tipo=tipo
        )
        texto = "Domínio aprovado."
    else:
        atualizado = db.atualizar_status_dominio(dominio, "rejeitado")
        texto = "Domínio rejeitado."
    if not atualizado:
        raise HTTPException(status_code=404, detail="Domínio não encontrado.")
    return RedirectResponse(
        f"/dominios?mensagem={quote(texto)}", status_code=303
    )


@app.get("/candidatos-imagem")
def candidatos_imagem_legado():
    return RedirectResponse("/valida", status_code=307)


@app.post("/valida/{candidate_id}/status", dependencies=_csrf())
def validar_candidato(
    request: Request,
    candidate_id: int,
    status: str = Form(...),
    target_user_id: int = Form(0),
):
    alvo = _usuario_alvo_busca(request, target_user_id)
    if status not in {"leaked", "not_me", "official"}:
        raise HTTPException(status_code=400, detail="Status de candidato inválido.")
    candidato = db.obter_candidato(candidate_id, alvo["id"])
    if candidato is None:
        raise HTTPException(status_code=404, detail="Candidato não encontrado.")
    db.atualizar_status_candidato(candidate_id, alvo["id"], status)
    try:
        dominios_core.registrar_feedback(candidato["url"], status)
    except Exception:
        logger.exception("Falha ao atualizar o feedback do domínio")
    if status == "leaked":
        db.adicionar_site_vazado(
            alvo["id"],
            request.state.user["id"],
            candidato["url"],
            candidato["title"],
            candidato["source"],
            candidato["snippet"],
            candidato["thumbnail"],
            candidato["engine"],
            {},
        )
    elif status == "official":
        url_oficial = pontuacao.normalizar_url(candidato["url"])
        if url_oficial:
            db.adicionar_pagina_oficial(
                alvo["id"],
                url_oficial,
                pontuacao.dominio_de(url_oficial),
                origem="revisao",
            )
        if candidato["phash"]:
            db.adicionar_hash_oficial(
                alvo["id"], candidato["url"], candidato["phash"]
            )
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"mensagem": "Candidato atualizado."})
    return RedirectResponse(
        f"/valida?target_user_id={alvo['id']}"
        f"&mensagem={quote('Candidato atualizado.')}",
        status_code=303,
    )


@app.get(
    "/execucoes",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def execucoes_de_busca(request: Request):
    """Historico por consulta: qual modelo trouxe o que em cada execucao."""
    return templates.TemplateResponse(
        request,
        "execucoes.html",
        {
            "execucoes": db.listar_execucoes(),
            "limiar": pontuacao.limiar_revisao(),
        },
    )


@app.get("/buscar-web")
def buscar_web_legado():
    return RedirectResponse("/busca-web", status_code=307)


@app.get(
    "/buscar-google",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def buscar_google_formulario():
    return RedirectResponse("/busca-web", status_code=307)


@app.post("/buscar-web", response_class=HTMLResponse, dependencies=_admin_csrf())
@app.post("/buscar-google", response_class=HTMLResponse, dependencies=_admin_csrf())
async def buscar_google_na_web(
    request: Request,
    conteudo_id: int = Form(...),
    consentimento: str = Form(""),
    provedor: str = Form("google_vision"),
    target_user_id: int = Form(0),
):
    if not isinstance(provedor, str):
        provedor = "google_vision"
    alvo = _usuario_alvo_busca(request, target_user_id)
    target_user_id = alvo["id"]
    contexto = {
        "target_user_id": target_user_id,
        "erro": "",
        "paginas": [],
        "imagens": [],
        "pesquisa_realizada": False,
        "conteudo_selecionado": conteudo_id,
        "provedor_imagem_selecionado": provedor,
    }
    if provedor != "google_vision":
        contexto["erro"] = "Este provedor ainda não está integrado nesta busca."
        return _contexto_google(request, **contexto)
    if consentimento != "on":
        contexto["erro"] = (
            "Confirme o envio da imagem ao Google Cloud Vision para iniciar a busca."
        )
        return _contexto_google(request, **contexto)

    conteudo = db.obter_conteudo(conteudo_id)
    if conteudo is None or conteudo["id"] not in {
        c["id"] for c in db.listar_conteudos(target_user_id)
    }:
        contexto["erro"] = "Conteúdo não encontrado no catálogo desta criadora."
        return _contexto_google(request, **contexto)

    nome_arquivo = conteudo["arquivo"]
    if Path(nome_arquivo).name != nome_arquivo:
        contexto["erro"] = "O caminho da imagem cadastrada é inválido."
        return _contexto_google(request, **contexto)
    caminho_arquivo = PASTA_OFICIAIS / nome_arquivo

    try:
        if caminho_arquivo.stat().st_size > TAMANHO_MAXIMO:
            raise ValueError("A imagem cadastrada é maior que 20 MB.")
        dados = caminho_arquivo.read_bytes()
        imagens.calcular_phash(dados)
    except OSError:
        logger.exception("Não foi possível ler a imagem do conteúdo %s", conteudo_id)
        contexto["erro"] = "Não foi possível ler a imagem selecionada no catálogo."
        return _contexto_google(request, **contexto)
    except ValueError as erro:
        contexto["erro"] = str(erro)
        return _contexto_google(request, **contexto)

    try:
        contexto["paginas"], contexto["imagens"] = await run_in_threadpool(
            busca_web.buscar_na_web, dados
        )
    except GoogleAuthError:
        logger.exception("Credenciais do Google Cloud Vision indisponíveis ou inválidas")
        contexto["erro"] = (
            "A busca não está configurada. O servidor precisa de credenciais "
            "do Google Cloud com acesso ao Vision API."
        )
    except GoogleAPIError:
        logger.exception("Falha na consulta ao Google Cloud Vision")
        contexto["erro"] = (
            "O Google Cloud Vision não concluiu a busca. Confira a API, as "
            "permissões e o faturamento do projeto."
        )
    except busca_web.ErroBuscaWeb:
        logger.exception("O Google Cloud Vision retornou um erro na busca")
        contexto["erro"] = "O Google Cloud Vision retornou um erro ao buscar a imagem."
    else:
        contexto["pesquisa_realizada"] = True

    if contexto["pesquisa_realizada"]:
        resultados = [
            {
                "link": pagina["url"],
                "titulo": pagina["titulo"],
                "miniatura": pagina["miniatura"],
            }
            for pagina in contexto["paginas"]
        ] + [
            {"link": imagem["url"], "titulo": imagem["tipo"], "miniatura": imagem["url"]}
            for imagem in contexto["imagens"]
        ]
        contexto["resumo_busca"] = await run_in_threadpool(
            _enviar_para_valida, alvo, resultados, "google_vision"
        )
    contexto["paginas"] = contexto["imagens"] = []
    return _contexto_google(request, **contexto)


def _enviar_para_valida(alvo, resultados, engine):
    """Filtra os resultados da API e envia os candidatos à página Valida.

    Omite páginas oficiais (ou filhas delas), as já marcadas como "não sou eu"
    e repetições. Só o hash da miniatura é guardado; a imagem é descartada.
    """
    user_id = alvo["id"]
    paginas_oficiais = [
        {"user_id": user_id, "url": pagina["url"]}
        for pagina in db.listar_paginas_oficiais(user_id)
    ]
    existentes = {c["url"]: c["status"] for c in db.listar_candidatos(user_id)}
    hashes_oficiais = {h["url"] for h in db.listar_hashes_oficiais(user_id)}
    posts_oficiais = {
        sites_oficiais.shortcode_instagram(u) for u in hashes_oficiais
    } - {""}
    resumo = {
        "total": len(resultados),
        "novos": 0,
        "oficiais": 0,
        "nao_sou_eu": 0,
        "repetidos": 0,
        "sem_hash": 0,
    }
    vistos = set()
    novos = []
    oficiais_proprios = []
    for resultado in resultados:
        url = sites_oficiais.url_pagina_da_busca(
            resultado.get("link") or resultado.get("url") or ""
        )
        if not url:
            continue
        if url in vistos:
            resumo["repetidos"] += 1
            continue
        vistos.add(url)
        dono = sites_oficiais.proprietario_pagina_oficial(
            url, paginas_oficiais, user_id
        )
        if dono != "nao_oficial":
            resumo["oficiais"] += 1
            if dono == "proprio" and url not in hashes_oficiais:
                oficiais_proprios.append((url, resultado))
            continue
        if sites_oficiais.shortcode_instagram(url) in posts_oficiais:
            resumo["oficiais"] += 1
            continue
        status = existentes.get(url)
        if status == "not_me":
            resumo["nao_sou_eu"] += 1
        elif status is not None:
            resumo["repetidos"] += 1
        else:
            novos.append((url, resultado))

    if comparacao_imagem_ativa():
        hashes = candidatos_core.calcular_hashes(
            [r["miniatura"] for _, r in novos + oficiais_proprios if r.get("miniatura")]
        )
    else:
        # Comparacao desligada: nenhuma midia e baixada nem processada.
        hashes = {}
        resumo["sem_hash"] = 0
    for url, resultado in oficiais_proprios:
        phash = hashes.get(resultado.get("miniatura"), "")
        if phash:
            db.adicionar_hash_oficial(user_id, url, phash)
    for url, resultado in novos:
        phash = hashes.get(resultado.get("miniatura"), "")
        if not phash:
            resumo["sem_hash"] += 1
        db.adicionar_candidato(
            user_id,
            url,
            (resultado.get("titulo") or "Resultado")[:500],
            (resultado.get("origem") or "")[:200],
            (resultado.get("resumo") or "")[:4000],
            resultado.get("miniatura") or "",
            engine,
            phash,
        )
        resumo["novos"] += 1
    return resumo


SERPAPI_ENGINES = {
    "google_images",
    "google_images_light",
    "google_images_related_content",
    "google_lens",
}
SERPAPI_ENGINE_NAMES = {
    "google_images": "Google Images API",
    "google_images_light": "Google Images Light API",
    "google_lens": "Google Lens API",
    "google_images_related_content": "Google Images Related Content API",
}


def _formatar_status_vazamento(status):
    if not status:
        return None
    formatado = dict(status)
    for chave in ("iniciado", "fim"):
        if isinstance(status.get(chave), (int, float)):
            formatado[chave] = datetime.fromtimestamp(
                status[chave], timezone.utc
            ).strftime("%d/%m/%Y %H:%M UTC")
    if (
        status.get("estado") == "rodando"
        and time.time() - status.get("iniciado", 0) > TEMPO_MAXIMO_BUSCA_VAZAMENTO
    ):
        formatado["estado"] = "erro"
        formatado["erro"] = "A busca não terminou (o servidor pode ter reiniciado)."
    return formatado


def _config_vazamentos():
    termos = db.obter_configuracao(
        "vazamento_termos", "\n".join(consultas_vazamento.TERMOS_PADRAO)
    )
    sites = db.obter_configuracao(
        "vazamento_sites", "\n".join(consultas_vazamento.SITES_PADRAO)
    )
    return (
        consultas_vazamento.limpar_termos(termos),
        consultas_vazamento.limpar_sites(sites),
    )


def _termos_busca(usuario):
    return f"{usuario['public_name']} @{usuario['public_handle']}"


def _contexto_busca_web(request: Request, target_user_id: int = 0, **valores):
    alvo = _usuario_alvo_busca(request, target_user_id)
    target_user_id = alvo["id"]
    fotos = db.listar_fotos_busca(target_user_id)
    contexto = {
        "erro": "",
        "mensagem": "",
        "api_ativa": "",
        "provedor_selecionado": "serpapi",
        "resultados": [],
        "query": "",
        "foto_selecionada": None,
        "related_content_id": "",
        "api_key_configurada": busca_provedores.configurado("serpapi"),
        "tinyfish_configurada": busca_provedores.configurado("tinyfish"),
        "comparacao_imagem_ativa": comparacao_imagem_ativa(),
        "nota_minima": pontuacao.limiar_revisao(),
        "busca_maxima_consultas": _limite_consultas_busca(),
        "arrobas_descobertos": db.listar_arrobas_descobertos(target_user_id),
        "status_conta": None,
        "status_conta_atualizado": False,
        "pesquisa_realizada": False,
        "related_options": [],
        "nomes_apis": {**SERPAPI_ENGINE_NAMES, **INSTAGRAM_ENGINE_NAMES},
        "fonte": "serpapi",
        "paginas": [],
        "imagens": [],
        "conteudos": db.listar_conteudos(target_user_id),
        "resumo_busca": None,
        "conteudo_selecionado": None,
        "provedores_imagem": provedores_busca.listar_provedores("imagem"),
        "provedor_imagem_selecionado": "google_vision",
        "username": "",
        "keyword": "",
        "perfil": {},
        "posts": [],
        "provedores": provedores_busca.listar_provedores("texto"),
        "target_user": alvo,
        "target_user_id": target_user_id,
        "target_users": (
            db.listar_usuarios_para_busca()
            if request.state.user["role"] == "admin"
            else []
        ),
        "perfil_busca_completo": bool(
            alvo["public_name"] and alvo["public_handle"]
        ),
        "fotos": fotos,
        "sites_vazados": db.listar_sites_vazados(target_user_id),
        "paginas_oficiais": db.listar_paginas_oficiais(target_user_id),
        "perfis_monitorados": db.listar_perfis_monitorados(target_user_id),
        "vazamento_termos": _config_vazamentos()[0],
        "vazamento_sites": _config_vazamentos()[1],
        "vazamento_periodos": consultas_vazamento.PERIODOS,
        "vazamento_status": _formatar_status_vazamento(
            _status_vazamento(target_user_id)
        ),
        "vazamento_consultas": (
            consultas_vazamento.gerar_consultas_priorizadas(
                _arrobas_para_busca(alvo),
                *_config_vazamentos(),
                _limite_consultas_busca(),
                nome=alvo["public_name"] or "",
            )
            if alvo["public_name"] and alvo["public_handle"]
            else []
        ),
        "query_perfil": (
            _termos_busca(alvo)
            if alvo["public_name"] and alvo["public_handle"]
            else ""
        ),
    }
    contexto.update(valores)
    _persistir_ou_restaurar_busca(request, contexto, valores)
    if not contexto["related_options"] and contexto["vazamento_status"]:
        contexto["related_options"] = contexto["vazamento_status"].get(
            "related_options", []
        )
    return templates.TemplateResponse(request, "busca_web.html", contexto)


CHAVES_SNAPSHOT_BUSCA = (
    "fonte", "api_ativa", "pesquisa_realizada", "resultados", "query",
    "related_options", "paginas", "imagens", "perfil", "posts", "username",
    "keyword", "conteudo_selecionado", "foto_selecionada", "resumo_busca",
)
# Chamadas que só mudam avisos ou o saldo não devem apagar a última busca.
CHAVES_SEM_BUSCA = {"erro", "mensagem", "status_conta", "status_conta_atualizado"}


def _persistir_ou_restaurar_busca(request: Request, contexto: dict, valores: dict):
    usuario_id = request.state.user["id"]
    alvo_id = contexto["target_user_id"]

    if "status_conta" in valores and valores["status_conta"]:
        db.salvar_snapshot_busca(usuario_id, "saldo", valores["status_conta"])
    elif not valores.get("status_conta"):
        saldo = db.obter_snapshot_busca(usuario_id, "saldo")
        if saldo:
            contexto["status_conta"] = saldo["dados"]
            contexto["status_conta_em"] = saldo["updated_at"]

    if valores.get("pesquisa_realizada"):
        dados = {chave: contexto[chave] for chave in CHAVES_SNAPSHOT_BUSCA}
        dados["target_user_id"] = alvo_id
        db.salvar_snapshot_busca(usuario_id, "busca", dados)
        return
    if set(valores) - CHAVES_SEM_BUSCA:
        return

    ultima = db.obter_snapshot_busca(usuario_id, "busca")
    if not ultima or ultima["dados"].get("target_user_id") != alvo_id:
        return
    dados = ultima["dados"]
    for chave in CHAVES_SNAPSHOT_BUSCA:
        if chave in dados:
            contexto[chave] = dados[chave]
    contexto["busca_restaurada_em"] = ultima["updated_at"]


def _contexto_serpapi(request: Request, target_user_id: int = 0, **valores):
    return _contexto_busca_web(request, target_user_id, **valores)


def _contexto_google(request: Request, **valores):
    return _contexto_busca_web(
        request, valores.pop("target_user_id", 0), fonte="google", **valores
    )


@app.get(
    "/buscar-serpapi",
    response_class=HTMLResponse,
)
def buscar_serpapi_formulario():
    return RedirectResponse("/busca-web", status_code=307)


@app.get(
    "/busca-web",
    response_class=HTMLResponse,
)
def pagina_busca_na_web(request: Request, target_user_id: int = 0):
    _usuario_alvo_busca(request, target_user_id)
    if request.state.user["role"] != "admin" or not os.getenv("SERPAPI_API_KEY", "").strip():
        return _contexto_serpapi(request, target_user_id=target_user_id)
    return _consultar_status_serpapi(request, target_user_id)


def _consultar_status_serpapi(request: Request, target_user_id: int):
    try:
        status_conta = serpapi.obter_status_conta()
    except serpapi.SerpApiNaoConfigurada as erro:
        return _contexto_serpapi(request, target_user_id, erro=str(erro))
    except serpapi.ErroSerpApi:
        logger.exception("Falha ao consultar o uso da conta SerpApi")
        return _contexto_serpapi(
            request,
            target_user_id,
            erro=(
                "Não foi possível consultar a conta SerpApi. "
                "Confira se a chave está correta e tente novamente."
            ),
        )
    return _contexto_serpapi(
        request,
        target_user_id,
        status_conta=status_conta,
        status_conta_atualizado=True,
    )


@app.post(
    "/status-serpapi",
    response_class=HTMLResponse,
    dependencies=_admin_csrf(),
)
async def status_serpapi(request: Request, target_user_id: int = Form(0)):
    if not os.getenv("SERPAPI_API_KEY", "").strip():
        return _contexto_serpapi(
            request,
            target_user_id,
            erro="Configure SERPAPI_API_KEY para consultar o uso da conta.",
        )
    _usuario_alvo_busca(request, target_user_id)
    return await run_in_threadpool(_consultar_status_serpapi, request, target_user_id)


@app.post("/buscar-serpapi", response_class=HTMLResponse, dependencies=_csrf())
async def buscar_serpapi(
    request: Request,
    api: str = Form(...),
    consentimento: str = Form(""),
    foto_id: int = Form(0),
    related_content_id: str = Form(""),
    target_user_id: int = Form(0),
    image_file: UploadFile | None = File(None),
    provedor: str = Form("serpapi"),
):
    if not isinstance(provedor, str):
        provedor = "serpapi"
    alvo = _usuario_alvo_busca(request, target_user_id)
    target_user_id = alvo["id"]
    query = (
        _termos_busca(alvo)
        if alvo["public_name"] and alvo["public_handle"]
        else ""
    )
    contexto = {
        "api_ativa": api,
        "provedor_selecionado": provedor,
        "query": query,
        "foto_selecionada": foto_id or None,
        "related_content_id": related_content_id.strip(),
    }
    if api not in SERPAPI_ENGINES:
        return _contexto_serpapi(
            request, target_user_id, erro="API selecionada inválida.", **contexto
        )
    if provedor != "serpapi":
        provedor_info = provedores_busca.obter_provedor(provedor)
        nome_provedor = provedor_info["nome"] if provedor_info else "desconhecido"
        return _contexto_serpapi(
            request,
            target_user_id,
            erro=(
                f"O provedor {nome_provedor} ainda não está integrado nesta busca."
            ),
            **contexto,
        )
    if consentimento != "on":
        return _contexto_serpapi(
            request,
            target_user_id,
            erro="Confirme o envio da consulta ou imagem à SerpApi.",
            **contexto,
        )
    if not query:
        return _contexto_serpapi(
            request,
            target_user_id,
            erro="Complete seu nome público e arroba no perfil de busca.",
            **contexto,
        )
    try:
        if api in {"google_images", "google_images_light"}:
            dados = await run_in_threadpool(
                serpapi.pesquisar, api, q=query, gl="br", hl="pt", safe="off"
            )
        elif api == "google_lens":
            if target_user_id != request.state.user["id"]:
                raise HTTPException(
                    status_code=403,
                    detail="A busca visual só pode ser feita pelo próprio usuário.",
                )
            if image_file and image_file.filename:
                foto_id, nome_arquivo, imagem = await _salvar_foto_busca(
                    target_user_id, image_file
                )
            elif foto_id:
                foto = db.obter_foto_busca(foto_id)
                if foto is None or foto["user_id"] != target_user_id:
                    raise ValueError("Selecione uma foto salva para esta conta.")
                nome_arquivo = foto["arquivo"]
                imagem = None
            else:
                raise ValueError("Escolha uma foto salva ou envie uma nova foto.")
            if Path(nome_arquivo).name != nome_arquivo:
                raise ValueError("O caminho da imagem salva é inválido.")
            caminho_arquivo = PASTA_FOTOS_BUSCA / nome_arquivo
            if not caminho_arquivo.is_file():
                raise ValueError("O arquivo da foto selecionada não foi encontrado.")
            if caminho_arquivo.stat().st_size > TAMANHO_MAXIMO:
                raise ValueError("A imagem selecionada é maior que 20 MB.")
            if imagem is None:
                imagem = caminho_arquivo.read_bytes()
            imagens.calcular_phash(imagem)
            contexto["foto_selecionada"] = foto_id
            dados = await run_in_threadpool(serpapi.pesquisar_imagem, imagem)
        else:
            if not contexto["related_content_id"]:
                raise ValueError(
                    "Use a opção de conteúdo relacionado em um resultado da Google Images."
                )
            dados = await run_in_threadpool(
                serpapi.pesquisar,
                api,
                q=query,
                related_content_id=contexto["related_content_id"],
                gl="br",
                hl="pt",
                safe="off",
            )
    except ValueError as erro:
        return _contexto_serpapi(
            request, target_user_id, erro=str(erro), **contexto
        )
    except serpapi.SerpApiNaoConfigurada as erro:
        return _contexto_serpapi(
            request, target_user_id, erro=str(erro), **contexto
        )
    except serpapi.ErroSerpApi as erro:
        logger.exception("Falha na consulta SerpApi (%s)", api)
        return _contexto_serpapi(
            request, target_user_id, erro=str(erro), **contexto
        )
    except OSError:
        logger.exception("Não foi possível ler uma foto de busca (%s)", foto_id)
        return _contexto_serpapi(
            request,
            target_user_id,
            erro="Não foi possível ler a imagem selecionada no catálogo.",
            **contexto,
        )

    resultados = serpapi.normalizar_resultados(dados, api)
    resumo_busca = await run_in_threadpool(
        _enviar_para_valida, alvo, resultados, api
    )
    related_options = _opcoes_relacionadas(dados) if api == "google_images" else []

    return _contexto_serpapi(
        request,
        target_user_id,
        pesquisa_realizada=True,
        resumo_busca=resumo_busca,
        query=query,
        api_ativa=api,
        related_options=related_options,
    )


@app.post(
    "/busca-vazamentos/config",
    response_class=HTMLResponse,
    dependencies=_admin_csrf(),
)
def salvar_config_vazamentos(
    request: Request,
    termos: str = Form(""),
    sites: str = Form(""),
    limite: int = Form(0),
    target_user_id: int = Form(0),
):
    db.salvar_configuracao(
        "vazamento_termos", "\n".join(consultas_vazamento.limpar_termos(termos))
    )
    db.salvar_configuracao(
        "vazamento_sites", "\n".join(consultas_vazamento.limpar_sites(sites))
    )
    if limite:
        db.salvar_configuracao(
            "busca_maxima_consultas", str(max(1, min(int(limite), 60)))
        )
    return _contexto_serpapi(
        request, target_user_id, mensagem="Termos, sites e limite salvos."
    )


TEMPO_MAXIMO_BUSCA_VAZAMENTO = 900


def _opcoes_relacionadas(dados):
    opcoes = []
    ids_vistos = set()
    for item in dados.get("images_results", []):
        if not isinstance(item, dict):
            continue
        related_id = item.get("related_content_id")
        if isinstance(related_id, str) and related_id and related_id not in ids_vistos:
            opcoes.append(
                {"id": related_id, "title": str(item.get("title") or "Resultado de imagem")}
            )
            ids_vistos.add(related_id)
    return opcoes


def _chave_status_vazamento(user_id):
    return f"vazamento_status_{user_id}"


def _status_vazamento(user_id):
    try:
        dados = json.loads(db.obter_configuracao(_chave_status_vazamento(user_id), "{}"))
    except ValueError:
        return {}
    return dados if isinstance(dados, dict) else {}


def _salvar_status_vazamento(user_id, dados):
    if not db.salvar_status_busca_usuario(
        user_id, _chave_status_vazamento(user_id), json.dumps(dados)
    ):
        logger.info("Status de busca descartado: usuário %s foi excluído", user_id)


def _executar_busca_vazamentos(alvo, api, consultas, parametros):
    """Roda as consultas fora da requisição (o plano gratuito é lento)."""
    user_id = alvo["id"]
    inicio = _status_vazamento(user_id).get("iniciado", time.time())
    base = {"iniciado": inicio, "consultas": consultas, "api": api}
    try:
        resultados = []
        por_consulta = []
        related_options = {}
        for consulta in consultas:
            try:
                dados = serpapi.pesquisar(api, q=consulta, **parametros)
                encontrados = serpapi.normalizar_resultados(dados, api)
                if api == "google_images":
                    for opcao in _opcoes_relacionadas(dados):
                        related_options[opcao["id"]] = opcao
            except serpapi.SemResultados:
                encontrados = []
            por_consulta.append({"consulta": consulta, "resultados": len(encontrados)})
            resultados.extend(encontrados)
        resumo = _enviar_para_valida(alvo, resultados, api)
        _salvar_status_vazamento(
            user_id,
            {
                **base,
                "estado": "ok",
                "resumo": resumo,
                "por_consulta": por_consulta,
                "related_options": list(related_options.values()),
                "fim": time.time(),
            },
        )
    except Exception as erro:
        logger.exception("Falha na busca de vazamentos (%s)", api)
        _salvar_status_vazamento(
            user_id,
            {**base, "estado": "erro", "erro": str(erro)[:300], "fim": time.time()},
        )


@app.post(
    "/buscar-vazamentos",
    response_class=HTMLResponse,
    dependencies=_csrf(),
)
async def buscar_vazamentos(
    request: Request,
    tarefas: BackgroundTasks,
    consentimento: str = Form(""),
    target_user_id: int = Form(0),
    api: str = Form("google_images_light"),
    periodo: str = Form(""),
    provedor: str = Form("serpapi"),
):
    """Inicia a busca no provedor escolhido, sempre com o Instagram primeiro."""
    if provedor not in busca_provedores.PROVEDORES_TEXTO:
        provedor = "serpapi"
    alvo = _usuario_alvo_busca(request, target_user_id)
    target_user_id = alvo["id"]
    contexto = {"api_ativa": api, "provedor_selecionado": provedor}

    def erro(mensagem):
        return _contexto_serpapi(request, target_user_id, erro=mensagem, **contexto)

    if provedor == "serpapi" and api not in {"google_images", "google_images_light"}:
        return erro("API selecionada inválida para o SerpAPI.")
    if periodo not in consultas_vazamento.PERIODOS:
        return erro("Período inválido.")
    if consentimento != "on":
        return erro(f"Confirme o envio das consultas ao {busca_provedores.PROVEDORES_NOMES[provedor]}.")
    if not (alvo["public_name"] and alvo["public_handle"]):
        return erro("Complete o nome público e a arroba no perfil de busca.")
    if not busca_provedores.configurado(provedor):
        variavel = "SERPAPI_API_KEY" if provedor == "serpapi" else "TINYFISH_API_KEY"
        return erro(f"Configure {variavel} no ambiente do servidor.")

    termos, sites = _config_vazamentos()
    limite = _limite_consultas_busca()
    consultas = consultas_vazamento.gerar_consultas_busca(
        _arrobas_para_busca(alvo),
        termos,
        sites,
        limite,
        nome=alvo["public_name"] or "",
    )
    if not consultas:
        return erro("Cadastre ao menos um termo ou site para pesquisar.")

    atual = _status_vazamento(target_user_id)
    if atual.get("estado") == "rodando" and (
        time.time() - atual.get("iniciado", 0) < TEMPO_MAXIMO_BUSCA_VAZAMENTO
    ):
        return erro("Já existe uma busca em andamento para esta criadora.")

    parametros = {"gl": "br", "hl": "pt", "safe": "off"}
    if periodo:
        parametros["tbs"] = f"qdr:{periodo}"
    _salvar_status_vazamento(
        target_user_id,
        {
            "estado": "rodando",
            "iniciado": time.time(),
            "consultas": consultas,
            "api": api,
            "provedor": provedor,
        },
    )
    tarefas.add_task(
        _executar_busca_provedor,
        alvo,
        provedor,
        termos,
        sites,
        limite,
        api,
        parametros,
    )
    return _contexto_serpapi(
        request,
        target_user_id,
        mensagem=(
            "Busca iniciada em segundo plano. O Instagram é consultado primeiro "
            "e os candidatos aparecem na Valida."
        ),
        api_ativa=api,
        provedor_selecionado=provedor,
    )


def _limite_consultas_busca() -> int:
    try:
        return max(1, min(int(db.obter_configuracao("busca_maxima_consultas", "18")), 60))
    except (TypeError, ValueError):
        return 18


def _arrobas_para_busca(alvo) -> list[str]:
    """Arrobas dos perfis configurados; contas antigas seguem como antes."""
    arrobas = perfis_core.arrobas_monitoradas(alvo["id"])
    if arrobas:
        return arrobas
    propria = (dict(alvo).get("public_handle") or "").strip().lstrip("@")
    if propria:
        arrobas.append(propria)
    for item in db.listar_arrobas_descobertos(alvo["id"]):
        arroba = (item.get("arroba") or "").strip().lstrip("@")
        if arroba and arroba.lower() not in {a.lower() for a in arrobas}:
            arrobas.append(arroba)
    return arrobas


def _executar_busca_provedor(alvo, provedor, termos, sites, limite, engine, parametros):
    """Roda em segundo plano: Instagram, consultas, pontuacao e persistencia."""
    user_id = alvo["id"]
    inicio = _status_vazamento(user_id).get("iniciado", time.time())
    try:
        resumo = fluxo_busca.executar(
            alvo,
            provedor,
            termos=termos,
            sites=sites,
            limite=limite,
            engine=engine,
            parametros=parametros,
        )
    except Exception as erro:
        logger.exception("Falha na busca de vazamentos (%s)", provedor)
        _salvar_status_vazamento(
            user_id,
            {
                "estado": "erro",
                "iniciado": inicio,
                "erro": str(erro)[:300],
                "fim": time.time(),
                "api": engine,
                "provedor": provedor,
            },
        )
        return
    estado = "erro" if resumo.get("erro") else "ok"
    _salvar_status_vazamento(
        user_id,
        {
            **resumo,
            "estado": estado,
            "iniciado": inicio,
            "fim": time.time(),
            "api": engine,
            "provedor": provedor,
            "resumo": resumo,
            "erro": resumo.get("erro", ""),
        },
    )


def _executar_busca_vazamentos(alvo, api, consultas, parametros):
    """Compatibilidade: executa uma lista de consultas pelo fluxo novo."""
    inicio = _status_vazamento(alvo["id"]).get("iniciado", time.time())
    termos, sites = _config_vazamentos()
    try:
        resumo = fluxo_busca.executar(
            alvo,
            "serpapi",
            termos=termos,
            sites=sites,
            limite=max(1, len(consultas)),
            engine=api,
            parametros=parametros,
            consultas=consultas,
        )
    except Exception as erro:
        logger.exception("Falha na busca de vazamentos (%s)", api)
        _salvar_status_vazamento(
            alvo["id"],
            {
                "estado": "erro",
                "iniciado": inicio,
                "erro": str(erro)[:300],
                "fim": time.time(),
                "api": api,
            },
        )
        return
    estado = "erro" if resumo.get("erro") else "ok"
    _salvar_status_vazamento(
        alvo["id"],
        {
            **resumo,
            "estado": estado,
            "iniciado": inicio,
            "fim": time.time(),
            "api": api,
            "resumo": resumo,
            "erro": resumo.get("erro", ""),
        },
    )


MAXIMO_PAGINAS_INSTAGRAM = 30

INSTAGRAM_ENGINE_NAMES = {
    "instagram_profile": "Instagram Profile API",
    "instagram_popular": "Instagram Popular API",
}


def _contexto_instagram(request: Request, **valores):
    return _contexto_busca_web(request, fonte="instagram", **valores)


@app.get(
    "/buscar-instagram",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def buscar_instagram_formulario():
    return RedirectResponse("/busca-web", status_code=307)


@app.post(
    "/instagram/importar-oficiais",
    response_class=HTMLResponse,
    dependencies=_admin_csrf(),
)
async def importar_posts_oficiais_instagram(
    request: Request,
    consentimento: str = Form(""),
    target_user_id: int = Form(0),
    username: str = Form(""),
    max_paginas: int = Form(MAXIMO_PAGINAS_INSTAGRAM),
):
    """Registra os posts do Instagram oficial da criadora (URL + hash)."""
    alvo = _usuario_alvo_busca(request, target_user_id)
    target_user_id = alvo["id"]
    contexto = {"api_ativa": "instagram_profile", "target_user_id": target_user_id}
    if consentimento != "on":
        return _contexto_instagram(
            request, erro="Confirme o envio da consulta à SerpApi.", **contexto
        )
    posts, paginas, token = [], 0, ""
    try:
        usuario = username.strip() or next(
            (
                u
                for u in (
                    sites_oficiais.usuario_instagram(p["url"])
                    for p in db.listar_paginas_oficiais(alvo["id"])
                )
                if u
            ),
            "",
        )
        if not usuario:
            raise ValueError(
                "Cadastre o Instagram oficial da criadora ou informe o usuário."
            )
        usuario = serpapi.validar_usuario_instagram(usuario)
        max_paginas = max(1, min(max_paginas, MAXIMO_PAGINAS_INSTAGRAM))
        vistos = set()
        while paginas < max_paginas:
            parametros = {"profile_id": usuario}
            if token:
                parametros["next_page_token"] = token
            dados = await run_in_threadpool(
                serpapi.pesquisar, "instagram_profile", **parametros
            )
            paginas += 1
            _, da_pagina = serpapi.normalizar_perfil_instagram(dados)
            novos = [p for p in da_pagina if p["link"] not in vistos]
            vistos.update(p["link"] for p in novos)
            posts.extend(novos)
            paginacao = dados.get("serpapi_pagination")
            proximo = paginacao.get("next_page_token") if isinstance(paginacao, dict) else ""
            if not novos or not isinstance(proximo, str) or not proximo:
                token = ""
                break
            token = proximo
    except (ValueError, serpapi.SerpApiNaoConfigurada, serpapi.ErroSerpApi) as erro:
        if not posts:
            return _contexto_instagram(request, erro=str(erro), **contexto)
        aviso_parcial = f"Importação interrompida: {erro}"
    else:
        aviso_parcial = ""

    if not posts:
        return _contexto_instagram(
            request,
            erro="A API não retornou publicações (o perfil pode ser privado).",
            **contexto,
        )
    hashes = await run_in_threadpool(
        candidatos_core.calcular_hashes,
        [p["miniatura"] for p in posts if p["miniatura"]],
    )
    codigos = set()
    for post in posts:
        codigo = sites_oficiais.shortcode_instagram(post["link"])
        codigos.add(codigo)
        db.adicionar_hash_oficial(
            alvo["id"], post["link"].rstrip("/"), hashes.get(post["miniatura"], "")
        )
    for candidato in db.listar_candidatos(alvo["id"], "pending"):
        if sites_oficiais.shortcode_instagram(candidato["url"]) in codigos:
            db.atualizar_status_candidato(candidato["id"], alvo["id"], "official")
    return _contexto_instagram(
        request,
        mensagem=(
            f"{len(posts)} publicações de @{usuario} registradas como oficiais "
            f"({paginas} crédito(s)); candidatos pendentes iguais foram marcados "
            "como oficiais."
            + (" Ainda há mais páginas: aumente o máximo de páginas." if token else "")
            + (f" {aviso_parcial}" if aviso_parcial else "")
        ),
        username=usuario,
        **contexto,
    )


@app.post("/buscar-instagram", response_class=HTMLResponse, dependencies=_admin_csrf())
async def buscar_instagram(
    request: Request,
    api: str = Form(...),
    consentimento: str = Form(""),
    username: str = Form(""),
    keyword: str = Form(""),
):
    contexto = {
        "api_ativa": api,
        "username": username.strip(),
        "keyword": keyword.strip(),
    }
    if api not in INSTAGRAM_ENGINE_NAMES:
        return _contexto_instagram(request, erro="API selecionada inválida.", **contexto)
    if consentimento != "on":
        return _contexto_instagram(
            request,
            erro="Confirme o envio da consulta à SerpApi.",
            **contexto,
        )

    try:
        if api == "instagram_profile":
            username_normalizado = serpapi.validar_usuario_instagram(username)
            dados = await run_in_threadpool(
                serpapi.pesquisar,
                api,
                profile_id=username_normalizado,
            )
            perfil, posts = serpapi.normalizar_perfil_instagram(dados)
        else:
            if not contexto["keyword"]:
                raise ValueError("Digite uma palavra-chave para pesquisar.")
            dados = await run_in_threadpool(
                serpapi.pesquisar,
                api,
                keyword=contexto["keyword"],
            )
            perfil = {}
            posts = serpapi.normalizar_instagram_popular(dados)
    except ValueError as erro:
        return _contexto_instagram(request, erro=str(erro), **contexto)
    except serpapi.SerpApiNaoConfigurada as erro:
        return _contexto_instagram(request, erro=str(erro), **contexto)
    except serpapi.ErroSerpApi as erro:
        logger.exception("Falha na consulta SerpApi Instagram (%s)", api)
        return _contexto_instagram(request, erro=str(erro), **contexto)

    sites_cadastrados = db.listar_sites_oficiais()
    posts = sites_oficiais.marcar_resultados(posts, sites_cadastrados)
    if perfil.get("link"):
        perfil = sites_oficiais.marcar_resultados(
            [perfil], sites_cadastrados
        )[0]
    return _contexto_instagram(
        request,
        pesquisa_realizada=True,
        perfil=perfil,
        posts=posts,
        api_ativa=api,
        username=contexto["username"],
        keyword=contexto["keyword"],
    )