"""Kit de remocao: modelos de pedido, status e prazos de checagem.

Os modelos ficam em arquivos de texto editaveis (`app/modelos_remocao/`), sem
base legal escrita por nos: os trechos juridicos sao preenchidos pela criadora
ou pelo advogado dela. O app so monta o texto com nome, arroba, links e
descricao.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

PASTA_MODELOS = Path("app/modelos_remocao")

STATUS = {
    "a_enviar": "A enviar",
    "enviado": "Enviado",
    "aguardando": "Aguardando resposta",
    "removido": "Removido",
    "recusado": "Recusado",
}
STATUS_PEDIDO = tuple(STATUS)
STATUS_ATIVOS = ("enviado", "aguardando")
STATUS_COM_PRAZO = STATUS_ATIVOS
ROTULOS_EVENTO = {**STATUS, "criado": "Pedido criado", "checagem": "Checagem"}

MODELOS = {"pt": "Português", "en": "Inglês"}
AVISO_MODELO = "Modelo a ser revisado por advogado."

GOOGLE_REMOCAO_URL = (
    "https://support.google.com/websearch/contact/content_removal_form?hl=pt-BR"
)
GOOGLE_REMOCAO_TEXTO = (
    "Pedir ao Google a remoção de um resultado com informações pessoais "
    "(formulário oficial, em português)"
)

DESCRICAO_PADRAO = (
    "Conteúdo com meu nome e meu arroba, publicado sem a minha autorização."
)
DIAS_CHECAGEM_PADRAO = 7


def rotulo_status(status: str) -> str:
    return ROTULOS_EVENTO.get(status or "", status or "")


def carregar_modelo(codigo: str) -> str:
    if codigo not in MODELOS:
        raise ValueError("Modelo de pedido inválido.")
    caminho = PASTA_MODELOS / f"{codigo}.txt"
    if not caminho.is_file():
        raise ValueError(f"Modelo {codigo} não encontrado em {PASTA_MODELOS}.")
    return caminho.read_text(encoding="utf-8")


def _lista_urls(urls) -> str:
    return "\n".join(f"- {url}" for url in urls if str(url).strip())


def gerar_texto(
    codigo: str,
    *,
    nome: str,
    arroba: str,
    urls,
    descricao: str = "",
    data: str = "",
) -> str:
    """Monta o pedido a partir do modelo editavel, trocando os marcadores."""
    texto = carregar_modelo(codigo)
    valores = {
        "{{nome}}": (nome or "").strip(),
        "{{arroba}}": (arroba or "").strip().lstrip("@"),
        "{{urls}}": _lista_urls(urls),
        "{{descricao}}": (descricao or "").strip() or DESCRICAO_PADRAO,
        "{{data}}": data or datetime.now(timezone.utc).strftime("%d/%m/%Y"),
    }
    for marcador, valor in valores.items():
        texto = texto.replace(marcador, valor)
    return texto


def dias_checagem() -> int:
    """Prazo da nova checagem: ambiente, depois app_settings, depois 7 dias."""
    import os

    do_ambiente = os.getenv("REMOCAO_DIAS_CHECAGEM", "").strip()
    if do_ambiente:
        try:
            return max(1, int(do_ambiente))
        except ValueError:
            pass
    try:
        from app import db

        return max(
            1,
            int(
                db.obter_configuracao(
                    "remocao_dias_checagem", str(DIAS_CHECAGEM_PADRAO)
                )
            ),
        )
    except (TypeError, ValueError):
        return DIAS_CHECAGEM_PADRAO


def proxima_checagem(dias: int | None = None) -> str:
    prazo = dias if dias and dias > 0 else dias_checagem()
    return (datetime.now(timezone.utc) + timedelta(days=prazo)).isoformat()


def prazo_vencido(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        quando = datetime.fromisoformat(str(iso))
    except ValueError:
        return False
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return quando <= datetime.now(timezone.utc)


def resumo_checagem(resultado: str, detalhes: dict | None = None) -> str:
    detalhes = detalhes or {}
    if resultado == "presente":
        return "A URL ainda apareceu na busca. Nada muda no pedido."
    if resultado == "ausente":
        return (
            "A URL não apareceu nesta busca. Isso é só um indício: confira com "
            "calma antes de marcar como removido."
        )
    if resultado == "erro":
        erros = [item.get("mensagem", "") for item in detalhes.values() if item.get("resultado") == "erro"]
        return erros[0] if erros else "Não foi possível concluir a checagem."
    return "Checagem registrada."
