import logging
import os
import uuid
from pathlib import Path
from urllib.parse import quote

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app import db
from app.core import busca_web, imagens, serpapi

PASTA_OFICIAIS = Path("media/oficiais")
EXTENSOES_PERMITIDAS = {".jpg", ".jpeg", ".png", ".webp"}
TAMANHO_MAXIMO = 20 * 1024 * 1024  # 20 MB
logger = logging.getLogger(__name__)

PASTA_OFICIAIS.mkdir(parents=True, exist_ok=True)
db.iniciar_banco()

app = FastAPI(title="Monitoramento de Vazamentos")
templates = Jinja2Templates(directory="app/templates")
app.mount("/arquivos", StaticFiles(directory=PASTA_OFICIAIS), name="arquivos")


async def _ler_imagem(arquivo: UploadFile) -> bytes:
    dados = await arquivo.read()
    if len(dados) > TAMANHO_MAXIMO:
        raise ValueError("O arquivo é maior que 20 MB.")
    return dados


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


@app.get("/catalogo", response_class=HTMLResponse)
def catalogo(request: Request, erro: str = "", mensagem: str = ""):
    contexto = {
        "criadoras": db.listar_criadoras(),
        "conteudos": db.listar_conteudos(),
        "erro": erro,
        "mensagem": mensagem,
    }
    return templates.TemplateResponse(request, "catalogo.html", contexto)


@app.post("/criadoras")
def nova_criadora(nome: str = Form(...)):
    nome = nome.strip()
    if nome:
        db.criar_criadora(nome)
    return RedirectResponse("/catalogo", status_code=303)


@app.post("/conteudos")
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


@app.post("/conteudos/{conteudo_id}/excluir")
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


@app.get("/comparar", response_class=HTMLResponse)
def comparar_formulario(request: Request):
    return templates.TemplateResponse(
        request, "comparar.html", {"resultados": None, "erro": ""}
    )


@app.post("/comparar", response_class=HTMLResponse)
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


@app.get("/buscar-web")
def buscar_web_legado():
    return RedirectResponse("/buscar-google", status_code=307)


@app.get("/buscar-google", response_class=HTMLResponse)
def buscar_google_formulario(request: Request):
    return templates.TemplateResponse(
        request,
        "buscar_web.html",
        {
            "erro": "",
            "paginas": [],
            "imagens": [],
            "pesquisa_realizada": False,
            "conteudos": db.listar_conteudos(),
            "conteudo_selecionado": None,
        },
    )


@app.post("/buscar-web", response_class=HTMLResponse)
@app.post("/buscar-google", response_class=HTMLResponse)
async def buscar_google_na_web(
    request: Request,
    conteudo_id: int = Form(...),
    consentimento: str = Form(""),
):
    contexto = {
        "erro": "",
        "paginas": [],
        "imagens": [],
        "pesquisa_realizada": False,
        "conteudos": db.listar_conteudos(),
        "conteudo_selecionado": conteudo_id,
    }
    if consentimento != "on":
        contexto["erro"] = (
            "Confirme o envio da imagem ao Google Cloud Vision para iniciar a busca."
        )
        return templates.TemplateResponse(request, "buscar_web.html", contexto)

    conteudo = db.obter_conteudo(conteudo_id)
    if conteudo is None:
        contexto["erro"] = "Conteúdo não encontrado no catálogo."
        return templates.TemplateResponse(request, "buscar_web.html", contexto)

    nome_arquivo = conteudo["arquivo"]
    if Path(nome_arquivo).name != nome_arquivo:
        contexto["erro"] = "O caminho da imagem cadastrada é inválido."
        return templates.TemplateResponse(request, "buscar_web.html", contexto)
    caminho_arquivo = PASTA_OFICIAIS / nome_arquivo

    try:
        if caminho_arquivo.stat().st_size > TAMANHO_MAXIMO:
            raise ValueError("A imagem cadastrada é maior que 20 MB.")
        dados = caminho_arquivo.read_bytes()
        imagens.calcular_phash(dados)
    except OSError:
        logger.exception("Não foi possível ler a imagem do conteúdo %s", conteudo_id)
        contexto["erro"] = "Não foi possível ler a imagem selecionada no catálogo."
        return templates.TemplateResponse(request, "buscar_web.html", contexto)
    except ValueError as erro:
        contexto["erro"] = str(erro)
        return templates.TemplateResponse(request, "buscar_web.html", contexto)

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

    return templates.TemplateResponse(request, "buscar_web.html", contexto)


SERPAPI_ENGINES = {
    "google_images",
    "google_images_light",
    "google_images_related_content",
    "google_lens",
    "google_reverse_image",
}
SERPAPI_ENGINE_NAMES = {
    "google_images": "Google Images API",
    "google_images_light": "Google Images Light API",
    "google_lens": "Google Lens API",
    "google_reverse_image": "Google Reverse Image API",
    "google_images_related_content": "Google Images Related Content API",
}


def _contexto_serpapi(request: Request, **valores):
    contexto = {
        "erro": "",
        "api_ativa": "",
        "resultados": [],
        "query": "",
        "conteudo_selecionado": None,
        "url_imagem": "",
        "related_content_id": "",
        "api_key_configurada": bool(os.getenv("SERPAPI_API_KEY", "").strip()),
        "pesquisa_realizada": False,
        "related_options": [],
        "nomes_apis": SERPAPI_ENGINE_NAMES,
        "conteudos": db.listar_conteudos(),
    }
    contexto.update(valores)
    return templates.TemplateResponse(request, "serpapi.html", contexto)


@app.get("/buscar-serpapi", response_class=HTMLResponse)
def buscar_serpapi_formulario(request: Request):
    return _contexto_serpapi(request)


@app.post("/buscar-serpapi", response_class=HTMLResponse)
async def buscar_serpapi(
    request: Request,
    api: str = Form(...),
    consentimento: str = Form(""),
    q: str = Form(""),
    conteudo_id: int = Form(0),
    image_url: str = Form(""),
    related_content_id: str = Form(""),
):
    contexto = {
        "api_ativa": api,
        "query": q.strip(),
        "conteudo_selecionado": conteudo_id or None,
        "url_imagem": image_url.strip(),
        "related_content_id": related_content_id.strip(),
    }
    if api not in SERPAPI_ENGINES:
        return _contexto_serpapi(request, erro="API selecionada inválida.", **contexto)
    if consentimento != "on":
        return _contexto_serpapi(
            request,
            erro="Confirme o envio da consulta ou imagem à SerpApi.",
            **contexto,
        )

    try:
        if api in {"google_images", "google_images_light"}:
            if not contexto["query"]:
                raise ValueError("Digite os termos que deseja pesquisar.")
            dados = await run_in_threadpool(
                serpapi.pesquisar, api, q=contexto["query"], gl="br", hl="pt"
            )
        elif api == "google_lens":
            conteudo = db.obter_conteudo(conteudo_id)
            if conteudo is None:
                raise ValueError("Selecione uma imagem existente no catálogo.")
            nome_arquivo = conteudo["arquivo"]
            if Path(nome_arquivo).name != nome_arquivo:
                raise ValueError("O caminho da imagem cadastrada é inválido.")
            caminho_arquivo = PASTA_OFICIAIS / nome_arquivo
            if caminho_arquivo.stat().st_size > TAMANHO_MAXIMO:
                raise ValueError("A imagem cadastrada é maior que 20 MB.")
            imagem = caminho_arquivo.read_bytes()
            imagens.calcular_phash(imagem)
            dados = await run_in_threadpool(serpapi.pesquisar_imagem, imagem)
        elif api == "google_reverse_image":
            if not serpapi.validar_url_imagem(contexto["url_imagem"]):
                raise ValueError(
                    "Informe uma URL pública de imagem começando com http:// ou https://."
                )
            dados = await run_in_threadpool(
                serpapi.pesquisar,
                api,
                image_url=contexto["url_imagem"],
                gl="br",
                hl="pt",
            )
        else:
            if not contexto["query"] or not contexto["related_content_id"]:
                raise ValueError(
                    "Use a opção de conteúdo relacionado em um resultado da Google Images."
                )
            dados = await run_in_threadpool(
                serpapi.pesquisar,
                api,
                q=contexto["query"],
                related_content_id=contexto["related_content_id"],
                gl="br",
                hl="pt",
            )
    except ValueError as erro:
        return _contexto_serpapi(request, erro=str(erro), **contexto)
    except serpapi.SerpApiNaoConfigurada as erro:
        return _contexto_serpapi(request, erro=str(erro), **contexto)
    except serpapi.ErroSerpApi as erro:
        logger.exception("Falha na consulta SerpApi (%s)", api)
        return _contexto_serpapi(request, erro=str(erro), **contexto)
    except OSError:
        logger.exception("Não foi possível ler a imagem do catálogo (%s)", conteudo_id)
        return _contexto_serpapi(
            request,
            erro="Não foi possível ler a imagem selecionada no catálogo.",
            **contexto,
        )

    resultados = serpapi.normalizar_resultados(dados, api)
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
        pesquisa_realizada=True,
        resultados=resultados,
        query=contexto["query"],
        api_ativa=api,
        related_options=related_options,
    )


INSTAGRAM_ENGINE_NAMES = {
    "instagram_profile": "Instagram Profile API",
    "instagram_popular": "Instagram Popular API",
}


def _contexto_instagram(request: Request, **valores):
    contexto = {
        "erro": "",
        "api_ativa": "",
        "username": "",
        "keyword": "",
        "api_key_configurada": bool(os.getenv("SERPAPI_API_KEY", "").strip()),
        "pesquisa_realizada": False,
        "perfil": {},
        "posts": [],
        "nomes_apis": INSTAGRAM_ENGINE_NAMES,
    }
    contexto.update(valores)
    return templates.TemplateResponse(request, "instagram.html", contexto)


@app.get("/buscar-instagram", response_class=HTMLResponse)
def buscar_instagram_formulario(request: Request):
    return _contexto_instagram(request)


@app.post("/buscar-instagram", response_class=HTMLResponse)
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

    return _contexto_instagram(
        request,
        pesquisa_realizada=True,
        perfil=perfil,
        posts=posts,
        api_ativa=api,
        username=contexto["username"],
        keyword=contexto["keyword"],
    )