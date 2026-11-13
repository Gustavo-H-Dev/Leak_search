import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.core import imagens

PASTA_OFICIAIS = Path("media/oficiais")
EXTENSOES_PERMITIDAS = {".jpg", ".jpeg", ".png", ".webp"}
TAMANHO_MAXIMO = 20 * 1024 * 1024  # 20 MB

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
def catalogo(request: Request, erro: str = ""):
    contexto = {
        "criadoras": db.listar_criadoras(),
        "conteudos": db.listar_conteudos(),
        "erro": erro,
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