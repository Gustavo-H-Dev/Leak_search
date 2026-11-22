import logging
import mimetypes
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

from app import db
from app.core import (
    autenticacao,
    busca_web,
    candidatos as candidatos_core,
    consultas_vazamento,
    imagens,
    provedores_busca,
    serpapi,
    sites_oficiais,
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

PASTA_OFICIAIS.mkdir(parents=True, exist_ok=True)
PASTA_FOTOS_BUSCA.mkdir(parents=True, exist_ok=True)
db.iniciar_banco()

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
    formulario = await request.form()
    enviado = formulario.get("csrf_token")
    esperado = request.session.get("csrf_token", "")
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
                else ""
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


@app.post("/cadastro", response_class=HTMLResponse, dependencies=_csrf())
async def cadastrar(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    nome_publico: str = Form(...),
    arroba: str = Form(...),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
):
    try:
        username = autenticacao.validar_usuario(username)
        email = autenticacao.validar_email(email)
        nome_publico = nome_publico.strip()
        if not nome_publico or len(nome_publico) > 80:
            raise ValueError("Informe seu nome público com até 80 caracteres.")
        arroba = serpapi.validar_usuario_instagram(arroba)
        if not db.perfil_criadora_disponivel(nome_publico, email):
            raise ValueError(
                "Esse nome já está no catálogo com outro e-mail. "
                "Peça ao administrador para atualizar o e-mail do perfil."
            )
        autenticacao.validar_senha(senha)
        if senha != confirmar_senha:
            raise ValueError("As senhas não coincidem.")
        if username.casefold() == ADMIN_USERNAME.casefold():
            raise ValueError("Esse nome de usuário está reservado.")
        if (
            autenticacao.confirmacao_email_obrigatoria()
            and not autenticacao.email_configurado()
        ):
            raise ValueError("O envio de e-mail ainda não está configurado.")
        user_id = db.criar_usuario(
            username,
            email,
            autenticacao.hash_senha(senha),
            nome_publico,
            arroba,
        )
        confirmacao_email_obrigatoria = autenticacao.confirmacao_email_obrigatoria()
        if not confirmacao_email_obrigatoria:
            db.confirmar_email(user_id)
    except ValueError as erro:
        return templates.TemplateResponse(
            request,
            "cadastro.html",
            {
                "erro": str(erro),
                "mensagem": "",
                "confirmacao_email_obrigatoria": autenticacao.confirmacao_email_obrigatoria(),
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


@app.get("/alterar-senha", response_class=HTMLResponse)
def alterar_senha_formulario(request: Request, erro: str = "", mensagem: str = ""):
    return templates.TemplateResponse(
        request,
        "alterar_senha.html",
        {"erro": erro, "mensagem": mensagem},
    )


@app.post("/alterar-senha", response_class=HTMLResponse, dependencies=_csrf())
def alterar_senha(
    request: Request,
    senha_atual: str = Form(...),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
):
    usuario = db.obter_usuario_por_login(request.state.user["username"])
    if not usuario or not autenticacao.verificar_senha(
        usuario["password_hash"], senha_atual
    ):
        return templates.TemplateResponse(
            request,
            "alterar_senha.html",
            {"erro": "A senha atual está incorreta.", "mensagem": ""},
            status_code=400,
        )
    try:
        autenticacao.validar_senha(senha)
        if senha != confirmar_senha:
            raise ValueError("As senhas não coincidem.")
    except ValueError as erro:
        return templates.TemplateResponse(
            request,
            "alterar_senha.html",
            {"erro": str(erro), "mensagem": ""},
            status_code=400,
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request, "home.html", {"titulo": "Monitoramento de Vazamentos"}
    )


@app.get("/saude")
def saude():
    return {"status": "ok"}


@app.get("/usuarios", response_class=HTMLResponse, dependencies=[Depends(exigir_admin)])
def usuarios(request: Request, erro: str = "", mensagem: str = ""):
    return templates.TemplateResponse(
        request,
        "usuarios.html",
        {
            "usuarios": db.listar_usuarios(),
            "erro": erro,
            "mensagem": mensagem,
        },
    )


@app.post(
    "/usuarios/{usuario_id}/perfil",
    dependencies=[Depends(exigir_admin), *_csrf()],
)
def atualizar_perfil_usuario(
    request: Request,
    usuario_id: int,
    role: str = Form(...),
):
    if usuario_id == request.state.user["id"]:
        return RedirectResponse(
            "/usuarios?erro=" + quote("Não é permitido alterar o próprio perfil."),
            status_code=303,
        )
    if role not in {"admin", "cliente"}:
        return RedirectResponse(
            "/usuarios?erro=" + quote("Perfil de usuário inválido."),
            status_code=303,
        )
    if not db.atualizar_perfil_usuario(usuario_id, role):
        return RedirectResponse(
            "/usuarios?erro=" + quote("Usuário não encontrado."),
            status_code=303,
        )
    return RedirectResponse(
        "/usuarios?mensagem=" + quote("Perfil atualizado."),
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
            "sites_oficiais": db.listar_sites_oficiais_usuario(usuario["id"]),
            "erro": erro,
            "mensagem": mensagem,
        },
    )


@app.post("/perfil-busca", dependencies=_csrf())
def salvar_perfil_busca(
    request: Request,
    nome_publico: str = Form(...),
    arroba: str = Form(...),
):
    nome_publico = nome_publico.strip()
    if not nome_publico or len(nome_publico) > 80:
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
    db.atualizar_perfil_busca(
        request.state.user["id"], nome_publico, arroba_normalizada
    )
    return RedirectResponse(
        "/perfil-busca?mensagem=" + quote("Perfil de busca atualizado."),
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


@app.post("/perfil-busca/sites-oficiais", dependencies=_csrf())
def adicionar_site_oficial_perfil(
    request: Request,
    nome: str = Form("Site oficial"),
    url: str = Form(...),
):
    nome = nome.strip() or "Site oficial"
    if len(nome) > 160:
        return RedirectResponse(
            "/perfil-busca?erro="
            + quote("O nome do site deve ter até 160 caracteres."),
            status_code=303,
        )
    try:
        url = sites_oficiais.normalizar_url_pagina(url)
    except ValueError as erro:
        return RedirectResponse(
            "/perfil-busca?erro=" + quote(str(erro)), status_code=303
        )
    user_id = request.state.user["id"]
    if not db.adicionar_site_oficial_usuario(user_id, user_id, nome, url):
        return RedirectResponse(
            "/perfil-busca?erro="
            + quote("Este site já está cadastrado como oficial."),
            status_code=303,
        )
    return RedirectResponse(
        "/perfil-busca?mensagem=" + quote("Site oficial adicionado."),
        status_code=303,
    )


@app.post("/perfil-busca/sites-oficiais/{site_id}/excluir", dependencies=_csrf())
def excluir_site_oficial_usuario(request: Request, site_id: int):
    user_id = request.state.user["id"]
    if not db.excluir_site_oficial_usuario(site_id, user_id):
        return RedirectResponse(
            "/perfil-busca?erro=" + quote("Site oficial não encontrado."),
            status_code=303,
        )
    return RedirectResponse(
        "/perfil-busca?mensagem=" + quote("Site oficial removido."),
        status_code=303,
    )


@app.get(
    "/catalogo",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def catalogo(request: Request, erro: str = "", mensagem: str = ""):
    sites_oficiais_cadastrados = db.listar_sites_oficiais()
    conteudos_cadastrados = db.listar_conteudos()
    criadoras = []
    for registro in db.listar_criadoras():
        criadora = dict(registro)
        criadora["sites_oficiais"] = [
            site
            for site in sites_oficiais_cadastrados
            if site["criadora_id"] == criadora["id"]
        ]
        criadora["conteudos"] = [
            conteudo
            for conteudo in conteudos_cadastrados
            if conteudo["criadora_id"] == criadora["id"]
        ]
        criadoras.append(criadora)
    contexto = {
        "criadoras": criadoras,
        "sites_sem_criadora": [
            site for site in sites_oficiais_cadastrados if not site["criadora_id"]
        ],
        "erro": erro,
        "mensagem": mensagem,
    }
    return templates.TemplateResponse(request, "catalogo.html", contexto)


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


@app.post("/criadoras", dependencies=_admin_csrf())
async def nova_criadora(
    nome: str = Form(...),
    site_nome: str = Form("Site oficial"),
    site_url: str = Form(""),
    arquivo: UploadFile | None = File(None),
    email: str = Form(""),
    arroba: str = Form(""),
):
    nome = nome.strip()
    if not nome:
        return _voltar_com_erro("Informe o nome da criadora.")
    if len(nome) > 80:
        return _voltar_com_erro("O nome da criadora deve ter até 80 caracteres.")

    email = email.strip() if isinstance(email, str) else ""
    if email:
        try:
            email = autenticacao.validar_email(email)
        except ValueError as erro:
            return _voltar_com_erro(str(erro))
    handle = ""
    if isinstance(arroba, str) and arroba.strip():
        try:
            handle = serpapi.validar_usuario_instagram(arroba)
        except ValueError as erro:
            return _voltar_com_erro(str(erro))

    url_oficial = None
    if site_url.strip():
        site_nome = site_nome.strip() or "Site oficial"
        if len(site_nome) > 80:
            return _voltar_com_erro(
                "O nome do site deve ter entre 1 e 80 caracteres."
            )
        try:
            url_oficial = sites_oficiais.normalizar_url_pagina(site_url)
        except ValueError as erro:
            return _voltar_com_erro(str(erro))

    imagem_nova = None
    if arquivo and arquivo.filename:
        extensao = Path(arquivo.filename).suffix.lower()
        if extensao not in EXTENSOES_PERMITIDAS:
            return _voltar_com_erro("Formato não suportado. Use JPG, PNG ou WEBP.")
        try:
            dados = await _ler_imagem(arquivo)
            phash = imagens.calcular_phash(dados)
        except ValueError as erro:
            return _voltar_com_erro(str(erro))
        imagem_nova = (dados, phash, extensao, arquivo.filename)

    try:
        if email or handle:
            criadora_id = db.criar_criadora_com_contato(nome, email, handle)
        else:
            criadora_id = db.criar_criadora(nome)
    except db.INTEGRITY_ERRORS:
        return _voltar_com_erro(
            "Já existe um perfil de criadora com esse nome ou e-mail."
        )
    if url_oficial:
        try:
            db.adicionar_site_oficial(criadora_id, site_nome, url_oficial)
        except db.INTEGRITY_ERRORS:
            return _voltar_com_erro(
                "Essa URL já está cadastrada para a criadora selecionada."
            )
    if imagem_nova:
        dados, phash, extensao, nome_original = imagem_nova
        nome_arquivo = f"{uuid.uuid4().hex}{extensao}"
        (PASTA_OFICIAIS / nome_arquivo).write_bytes(dados)
        db.adicionar_conteudo(
            criadora_id, "foto", nome_arquivo, nome_original, [phash]
        )
    return RedirectResponse("/catalogo", status_code=303)


@app.post("/criadoras/{criadora_id}/editar", dependencies=_admin_csrf())
def editar_criadora(
    criadora_id: int,
    nome: str = Form(...),
    email: str = Form(""),
    arroba: str = Form(""),
):
    nome = nome.strip()
    if not nome or len(nome) > 80:
        return _voltar_com_erro("O nome da criadora deve ter entre 1 e 80 caracteres.")
    email = email.strip()
    if email:
        try:
            email = autenticacao.validar_email(email)
        except ValueError as erro:
            return _voltar_com_erro(str(erro))
    handle = ""
    if arroba.strip():
        try:
            handle = serpapi.validar_usuario_instagram(arroba)
        except ValueError as erro:
            return _voltar_com_erro(str(erro))
    try:
        atualizado = db.atualizar_criadora_contato(
            criadora_id, nome, email, handle
        )
    except db.INTEGRITY_ERRORS:
        return _voltar_com_erro(
            "Já existe um perfil de criadora com esse nome ou e-mail."
        )
    if not atualizado:
        return _voltar_com_erro(
            "Perfil não encontrado ou já associado a uma conta."
        )
    return RedirectResponse(
        "/catalogo?mensagem=" + quote("Perfil de criadora atualizado."),
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


@app.get(
    "/comparar",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def comparar_formulario(request: Request):
    return templates.TemplateResponse(
        request, "comparar.html", {"resultados": None, "erro": ""}
    )


@app.post("/comparar", response_class=HTMLResponse, dependencies=_admin_csrf())
async def comparar(request: Request, arquivo: UploadFile = File(...)):
    try:
        dados = await _ler_imagem(arquivo)
        phash = imagens.calcular_phash(dados)
    except ValueError as erro:
        return templates.TemplateResponse(
            request, "comparar.html", {"resultados": None, "erro": str(erro)}
        )
    resultados = imagens.melhores_correspondencias(phash, db.listar_hashes())
    return templates.TemplateResponse(
        request,
        "comparar.html",
        {"resultados": resultados, "erro": "", "nome_arquivo": arquivo.filename},
    )


def _referencias_do_usuario(alvo):
    """Hashes de referência: catálogo, fotos do perfil e sites oficiais."""
    referencias = [
        {"phash": linha["phash"], "origem": f"Catálogo — {linha['criadora']}"}
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
    )
    return referencias


STATUS_VALIDA = {
    "pending": "Pendentes",
    "leaked": "Vazados",
    "not_me": "Não sou eu",
    "official": "Oficiais",
}


@app.get("/valida", response_class=HTMLResponse)
def valida(
    request: Request,
    target_user_id: int = 0,
    status: str = "pending",
    mensagem: str = "",
):
    alvo = _usuario_alvo_busca(request, target_user_id)
    if status not in STATUS_VALIDA:
        status = "pending"
    referencias = _referencias_do_usuario(alvo)
    candidatos = []
    for candidato in db.listar_candidatos(alvo["id"], status):
        melhor = candidatos_core.melhor_referencia(candidato["phash"], referencias)
        distancia = melhor["distancia"] if melhor else None
        listagem = sites_oficiais.pagina_de_listagem(candidato["url"])
        candidatos.append(
            {
                **candidato,
                "distancia": distancia,
                "pontuacao": (
                    imagens.pontuacao(distancia) if distancia is not None else None
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
    candidatos = candidatos_core.ordenar_por_atencao(candidatos)
    return templates.TemplateResponse(
        request,
        "valida.html",
        {
            "target_user": alvo,
            "target_user_id": alvo["id"],
            "target_users": (
                db.listar_usuarios_com_perfil_busca()
                if request.state.user["role"] == "admin"
                else []
            ),
            "candidatos": candidatos,
            "pontuacao_atencao": imagens.pontuacao(imagens.LIMITE_PROVAVEL),
            "pontuacao_possivel": imagens.pontuacao(imagens.LIMITE_POSSIVEL),
            "status_atual": status,
            "status_nomes": STATUS_VALIDA,
            "contagens": db.contar_candidatos_por_status(alvo["id"]),
            "tem_referencias": bool(referencias),
            "mensagem": mensagem,
        },
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
        db.adicionar_site_oficial_usuario(
            alvo["id"],
            request.state.user["id"],
            candidato["title"][:160] or "Site oficial",
            candidato["url"],
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
    paginas_oficiais = db.listar_paginas_oficiais_com_donos()
    existentes = {c["url"]: c["status"] for c in db.listar_candidatos(user_id)}
    hashes_oficiais = {h["url"] for h in db.listar_hashes_oficiais(user_id)}
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
        status = existentes.get(url)
        if status == "not_me":
            resumo["nao_sou_eu"] += 1
        elif status is not None:
            resumo["repetidos"] += 1
        else:
            novos.append((url, resultado))

    hashes = candidatos_core.calcular_hashes(
        [r["miniatura"] for _, r in novos + oficiais_proprios if r.get("miniatura")]
    )
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
        "api_key_configurada": bool(os.getenv("SERPAPI_API_KEY", "").strip()),
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
            db.listar_usuarios_com_perfil_busca()
            if request.state.user["role"] == "admin"
            else []
        ),
        "perfil_busca_completo": bool(
            alvo["public_name"] and alvo["public_handle"]
        ),
        "fotos": fotos,
        "sites_vazados": db.listar_sites_vazados(target_user_id),
        "vazamento_termos": _config_vazamentos()[0],
        "vazamento_sites": _config_vazamentos()[1],
        "vazamento_periodos": consultas_vazamento.PERIODOS,
        "vazamento_consultas": (
            consultas_vazamento.montar_consultas(
                alvo["public_name"], alvo["public_handle"], *_config_vazamentos()
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
    dependencies=[Depends(exigir_admin)],
)
def buscar_serpapi_formulario():
    return RedirectResponse("/busca-web", status_code=307)


@app.get(
    "/busca-web",
    response_class=HTMLResponse,
    dependencies=[Depends(exigir_admin)],
)
def pagina_busca_na_web(request: Request, target_user_id: int = 0):
    return _contexto_serpapi(request, target_user_id=target_user_id)


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
    try:
        status_conta = await run_in_threadpool(serpapi.obter_status_conta)
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


@app.post("/buscar-serpapi", response_class=HTMLResponse, dependencies=_admin_csrf())
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
                serpapi.pesquisar, api, q=query, gl="br", hl="pt"
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
    related_options = []
    if api == "google_images":
        ids_vistos = set()
        for item in dados.get("images_results", []):
            if not isinstance(item, dict):
                continue
            related_id = item.get("related_content_id")
            if isinstance(related_id, str) and related_id and related_id not in ids_vistos:
                related_options.append(
                    {
                        "id": related_id,
                        "title": str(item.get("title") or "Resultado de imagem"),
                    }
                )
                ids_vistos.add(related_id)

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
    target_user_id: int = Form(0),
):
    db.salvar_configuracao(
        "vazamento_termos", "\n".join(consultas_vazamento.limpar_termos(termos))
    )
    db.salvar_configuracao(
        "vazamento_sites", "\n".join(consultas_vazamento.limpar_sites(sites))
    )
    return _contexto_serpapi(
        request, target_user_id, mensagem="Termos e sites salvos."
    )


@app.post(
    "/buscar-vazamentos",
    response_class=HTMLResponse,
    dependencies=_admin_csrf(),
)
async def buscar_vazamentos(
    request: Request,
    consentimento: str = Form(""),
    target_user_id: int = Form(0),
    api: str = Form("google_images_light"),
    periodo: str = Form(""),
):
    alvo = _usuario_alvo_busca(request, target_user_id)
    target_user_id = alvo["id"]
    contexto = {"api_ativa": api}

    def erro(mensagem):
        return _contexto_serpapi(request, target_user_id, erro=mensagem, **contexto)

    if api not in {"google_images", "google_images_light"}:
        return erro("API selecionada inválida.")
    if periodo not in consultas_vazamento.PERIODOS:
        return erro("Período inválido.")
    if consentimento != "on":
        return erro("Confirme o envio das consultas à SerpApi.")
    if not (alvo["public_name"] and alvo["public_handle"]):
        return erro("Complete o nome público e a arroba no perfil de busca.")
    consultas = consultas_vazamento.montar_consultas(
        alvo["public_name"], alvo["public_handle"], *_config_vazamentos()
    )
    if not consultas:
        return erro("Cadastre ao menos um termo ou site para pesquisar.")

    parametros = {"gl": "br", "hl": "pt"}
    if periodo:
        parametros["tbs"] = f"qdr:{periodo}"
    resultados = []
    try:
        for consulta in consultas:
            dados = await run_in_threadpool(
                serpapi.pesquisar, api, q=consulta, **parametros
            )
            resultados.extend(serpapi.normalizar_resultados(dados, api))
    except serpapi.ErroSerpApi as e:
        logger.exception("Falha na busca de vazamentos (%s)", api)
        return erro(str(e))

    resumo_busca = await run_in_threadpool(
        _enviar_para_valida, alvo, resultados, api
    )
    return _contexto_serpapi(
        request,
        target_user_id,
        pesquisa_realizada=True,
        resumo_busca=resumo_busca,
        api_ativa=api,
        query=" | ".join(consultas),
    )


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