"""Perfis, variantes e paginas oficiais que a criadora configura.

Este modulo so orquestra: validacao/normalizacao dos dados e o estado do
onboarding. A persistencia fica em `app.db` e a geracao das variantes continua
em `app.core.variantes_nome` (nada foi reimplementado).
"""

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from app import db
from app.core import pontuacao, variantes_nome

# Plataformas com perfil publico que a busca sabe reconhecer como oficial.
PLATAFORMAS = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "x": "X (Twitter)",
    "youtube": "YouTube",
    "onlyfans": "OnlyFans",
    "privacy": "Privacy",
    "fansly": "Fansly",
    "telegram": "Telegram",
    "linktree": "Linktree",
    "threads": "Threads",
    "facebook": "Facebook",
    "twitch": "Twitch",
    "kick": "Kick",
    "snapchat": "Snapchat",
    "bluesky": "Bluesky",
}

MODELOS_DE_URL = {
    "instagram": "https://instagram.com/{}",
    "tiktok": "https://tiktok.com/@{}",
    "x": "https://x.com/{}",
    "youtube": "https://youtube.com/@{}",
    "onlyfans": "https://onlyfans.com/{}",
    "privacy": "https://privacy.com.br/@{}",
    "fansly": "https://fansly.com/{}",
    "telegram": "https://t.me/{}",
    "linktree": "https://linktr.ee/{}",
    "threads": "https://threads.net/@{}",
    "facebook": "https://facebook.com/{}",
    "twitch": "https://twitch.tv/{}",
    "kick": "https://kick.com/{}",
    "snapchat": "https://snapchat.com/add/{}",
    "bluesky": "https://bsky.app/profile/{}",
}

_ARROBA_VALIDA = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,29})[a-z0-9]$")

PASSOS = (
    (1, "Perfis", "Arrobas monitorados em cada plataforma"),
    (2, "Variações", "Formas do seu nome que aparecem nos títulos"),
    (3, "Páginas oficiais", "Páginas que devem ser ignoradas na pontuação"),
    (4, "Resumo", "Confira e faça a primeira busca"),
)


def _chave_estado(nome: str, criadora_id: int) -> str:
    return f"{nome}:{criadora_id}"


def normalizar_arroba(arroba: str) -> str:
    """Arroba minusculo, sem @ e sem espacos."""
    return (arroba or "").strip().lstrip("@").strip().lower()


def validar_arroba(plataforma: str, arroba: str) -> str:
    plataforma = (plataforma or "").strip().lower()
    if plataforma not in PLATAFORMAS:
        raise ValueError("Plataforma desconhecida.")
    normalizado = normalizar_arroba(arroba)
    if not normalizado:
        raise ValueError("Informe a arroba do perfil.")
    if not _ARROBA_VALIDA.match(normalizado):
        raise ValueError(
            "A arroba deve ter de 2 a 31 caracteres, usando letras, números, "
            "ponto, hífen ou sublinhado."
        )
    return normalizado


def url_do_perfil(plataforma: str, arroba: str) -> str:
    modelo = MODELOS_DE_URL.get((plataforma or "").lower())
    if not modelo:
        return ""
    return modelo.format(arroba)


def normalizar_pagina(url: str) -> tuple[str, str]:
    """(url normalizada, dominio). Levanta ValueError se a URL nao servir.

    Parametros de consulta e fragmento saem: a pagina oficial e a mesma
    independentemente do `?hl=pt` que o navegador acrescenta.
    """
    normalizada = pontuacao.normalizar_url(url)
    if normalizada:
        partes = urlparse(normalizada)
        normalizada = f"{partes.scheme}://{partes.netloc}{partes.path.rstrip('/')}"
    dominio = pontuacao.dominio_de(normalizada)
    if not normalizada or not dominio:
        raise ValueError("Informe uma URL pública válida, como https://seu-site.com.")
    if len(normalizada) > 500:
        raise ValueError("URL longa demais.")
    return normalizada, dominio


def salvar_perfil(criadora_id: int, plataforma: str, arroba: str) -> bool:
    """Cria ou reativa um perfil monitorado. False quando ja existia ativo."""
    arroba = validar_arroba(plataforma, arroba)
    existentes = db.listar_perfis_monitorados(criadora_id)
    for perfil in existentes:
        if (perfil["plataforma"], perfil["arroba"]) == (plataforma, arroba):
            if perfil["status"] != "ativo":
                db.atualizar_status_perfil_monitorado(perfil["id"], criadora_id, "ativo")
                return True
            return False
    if len(existentes) >= db.limite_perfis_monitorados():
        raise ValueError(
            f"O limite de {db.limite_perfis_monitorados()} perfis monitorados "
            "já foi atingido. Remova um perfil ou pause antes de adicionar outro."
        )
    db.adicionar_perfil_monitorado(criadora_id, plataforma, arroba)
    return True


def arrobas_monitoradas(criadora_id: int, apenas_ativos: bool = True) -> list[str]:
    status = "ativo" if apenas_ativos else None
    return [
        perfil["arroba"]
        for perfil in db.listar_perfis_monitorados(criadora_id, status=status)
    ]


def sugerir_variantes(criadora_id: int) -> list[str]:
    """Variantes geradas a partir das arrobas monitoradas.

    Reaproveita `variantes_nome.variantes_dos_arrobas`; quando nao ha perfil
    configurado ainda, usa o @ do cadastro para nao deixar o passo vazio.
    """
    arrobas = arrobas_monitoradas(criadora_id)
    if not arrobas:
        usuario = db.obter_usuario_por_id(criadora_id)
        if usuario and usuario["public_handle"]:
            arrobas = [usuario["public_handle"]]
    return variantes_nome.variantes_dos_arrobas(arrobas)


def salvar_variantes(criadora_id: int, textos, origem: str = "gerada") -> int:
    """Grava variantes novas e devolve quantas entraram."""
    total = 0
    for texto in textos:
        limpo = " ".join((texto or "").split()).lower()
        if not limpo or len(limpo) < 2 or len(limpo) > 80:
            continue
        if db.adicionar_variante_nome(criadora_id, limpo, origem=origem):
            total += 1
    return total


def salvar_pagina(criadora_id: int, url: str, origem: str = "manual") -> bool:
    normalizada, dominio = normalizar_pagina(url)
    if len(db.listar_paginas_oficiais(criadora_id)) >= LIMITE_PAGINAS:
        raise ValueError(
            f"Você já cadastrou {LIMITE_PAGINAS} páginas oficiais, o máximo por conta."
        )
    return db.adicionar_pagina_oficial(criadora_id, normalizada, dominio, origem) is not None


LIMITE_PAGINAS = 20


def variantes_para_pontuar(criadora_id: int) -> list[str]:
    """Apenas as variantes ativas; contas sem onboarding seguem como antes."""
    linhas = db.listar_variantes_nome(criadora_id)
    if not linhas:
        return variantes_nome.variantes_dos_arrobas(arrobas_monitoradas(criadora_id))
    return [linha["texto"] for linha in linhas if int(linha["ativo"] or 0)]


def paginas_para_ignorar(criadora_id: int) -> list[str]:
    return [pagina["url"] for pagina in db.listar_paginas_oficiais(criadora_id)]


def estado(criadora_id: int) -> dict:
    """Progresso do onboarding derivado dos dados salvos (retomavel)."""
    perfis = db.listar_perfis_monitorados(criadora_id)
    variantes = db.listar_variantes_nome(criadora_id)
    paginas = db.listar_paginas_oficiais(criadora_id)
    pulou_oficiais = bool(
        db.obter_configuracao(_chave_estado("onboarding_oficiais_puladas", criadora_id))
    )
    variantes_vistas = bool(
        db.obter_configuracao(_chave_estado("onboarding_variantes_vistas", criadora_id))
    )
    concluido = db.obter_configuracao(
        _chave_estado("onboarding_concluido", criadora_id)
    )

    status = {
        1: bool(perfis),
        2: bool(variantes) or variantes_vistas,
        3: bool(paginas) or pulou_oficiais,
        4: bool(concluido),
    }
    passos = [
        {
            "numero": numero,
            "titulo": titulo,
            "descricao": descricao,
            "concluido": status[numero],
        }
        for numero, titulo, descricao in PASSOS
    ]
    atual = next((numero for numero in (1, 2, 3, 4) if not status[numero]), 4)
    return {
        "passos": passos,
        "atual": atual,
        "concluido": bool(concluido),
        "perfis": perfis,
        "variantes": variantes,
        "paginas": paginas,
        "pulou_oficiais": pulou_oficiais,
    }


def concluir(criadora_id: int) -> None:
    db.salvar_configuracao(
        _chave_estado("onboarding_concluido", criadora_id),
        datetime.now(timezone.utc).isoformat(),
    )


def marcar_variantes_vistas(criadora_id: int) -> None:
    """A etapa 2 e opcional: passar por ela ja conta como concluida."""
    db.salvar_configuracao(
        _chave_estado("onboarding_variantes_vistas", criadora_id), "1"
    )


def pular_oficiais(criadora_id: int) -> None:
    db.salvar_configuracao(
        _chave_estado("onboarding_oficiais_puladas", criadora_id), "1"
    )
