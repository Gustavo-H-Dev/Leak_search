import io
import os
import re
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

API_URL = "https://serpapi.com/search.json"
UPLOAD_URL = "https://serpapi.com/image"
ACCOUNT_API_URL = "https://serpapi.com/account.json"
TAMANHO_MAXIMO_UPLOAD = 500 * 1024
TIMEOUT = 30


class ErroSerpApi(Exception):
    pass


class SemResultados(ErroSerpApi):
    pass


class SerpApiNaoConfigurada(ErroSerpApi):
    pass


def _api_key() -> str:
    chave = os.getenv("SERPAPI_API_KEY", "").strip()
    if not chave:
        raise SerpApiNaoConfigurada(
            "Configure a variável SERPAPI_API_KEY no ambiente do servidor."
        )
    return chave


def validar_url_imagem(url: str) -> bool:
    try:
        partes = urlsplit(url)
    except ValueError:
        return False
    return partes.scheme in {"http", "https"} and bool(partes.netloc)


def validar_usuario_instagram(usuario: str) -> str:
    usuario = usuario.strip().removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", usuario):
        raise ValueError("Informe somente o nome de usuário do Instagram.")
    return usuario


def _otimizar_imagem(conteudo: bytes) -> bytes:
    try:
        imagem = ImageOps.exif_transpose(Image.open(io.BytesIO(conteudo)))
        imagem = imagem.convert("RGB")
    except (UnidentifiedImageError, OSError) as erro:
        raise ErroSerpApi("A imagem selecionada não é válida.") from erro

    largura, altura = imagem.size
    while True:
        for qualidade in (88, 78, 68, 58):
            buffer = io.BytesIO()
            imagem.save(buffer, format="JPEG", quality=qualidade, optimize=True)
            dados = buffer.getvalue()
            if len(dados) <= TAMANHO_MAXIMO_UPLOAD:
                return dados
        largura = max(1, int(largura * 0.8))
        altura = max(1, int(altura * 0.8))
        if largura < 32 or altura < 32:
            raise ErroSerpApi(
                "Não foi possível reduzir a imagem para o limite de 500 KB da SerpApi."
            )
        imagem = imagem.resize((largura, altura), Image.Resampling.LANCZOS)


def _validar_resposta(resposta: httpx.Response) -> dict:
    try:
        resposta.raise_for_status()
    except httpx.HTTPStatusError as erro:
        raise ErroSerpApi(
            f"A SerpApi respondeu com HTTP {resposta.status_code}."
        ) from erro
    try:
        dados = resposta.json()
    except ValueError as erro:
        raise ErroSerpApi("A SerpApi retornou uma resposta inválida.") from erro
    if not isinstance(dados, dict):
        raise ErroSerpApi("A SerpApi retornou uma resposta inválida.")
    if dados.get("error"):
        if "hasn't returned any results" in str(dados["error"]):
            raise SemResultados(str(dados["error"]))
        raise ErroSerpApi(str(dados["error"]))
    return dados


def pesquisar(engine: str, **parametros: str) -> dict:
    chave = _api_key()
    try:
        resposta = httpx.get(
            API_URL,
            params={"engine": engine, "api_key": chave, **parametros},
            timeout=TIMEOUT,
        )
    except httpx.RequestError as erro:
        raise ErroSerpApi("Não foi possível conectar à SerpApi.") from erro
    return _validar_resposta(resposta)


def obter_status_conta() -> dict:
    chave = _api_key()
    try:
        resposta = httpx.get(
            ACCOUNT_API_URL,
            params={"api_key": chave},
            timeout=TIMEOUT,
        )
    except httpx.RequestError as erro:
        raise ErroSerpApi("Não foi possível consultar a conta SerpApi.") from erro

    dados = _validar_resposta(resposta)
    campos = (
        "account_status",
        "plan_name",
        "searches_per_month",
        "plan_searches_left",
        "extra_credits",
        "total_searches_left",
        "this_month_usage",
        "plan_renewal_date",
    )
    return {campo: dados[campo] for campo in campos if campo in dados}


def pesquisar_imagem(conteudo: bytes) -> dict:
    chave = _api_key()
    imagem = _otimizar_imagem(conteudo)
    try:
        upload = httpx.post(
            UPLOAD_URL,
            data={"api_key": chave},
            files={"image": ("imagem.jpg", imagem, "image/jpeg")},
            timeout=TIMEOUT,
        )
    except httpx.RequestError as erro:
        raise ErroSerpApi("Não foi possível enviar a imagem à SerpApi.") from erro

    dados_upload = _validar_resposta(upload)
    image_id = dados_upload.get("image_id")
    if not isinstance(image_id, str) or not image_id:
        raise ErroSerpApi("A SerpApi não retornou o identificador da imagem.")

    return pesquisar("google_lens", image_id=image_id)


def normalizar_resultados(dados: dict, engine: str) -> list[dict[str, str]]:
    chaves = {
        "google_images": ("images_results",),
        "google_images_light": ("images_results",),
        "google_lens": ("visual_matches", "exact_matches"),
        "google_reverse_image": ("image_results", "inline_images"),
        "google_images_related_content": ("related_content",),
    }
    resultados = []
    links_vistos = set()
    for chave in chaves[engine]:
        for item in dados.get(chave, []):
            if not isinstance(item, dict):
                continue
            link = item.get("link") or item.get("url") or item.get("original")
            if not isinstance(link, str) or not validar_url_imagem(link):
                continue
            if link in links_vistos:
                continue
            links_vistos.add(link)
            resultado = {
                "titulo": str(item.get("title") or item.get("name") or "Resultado"),
                "link": link,
                "provedor": "SerpApi",
                "origem": str(item.get("source") or ""),
                "miniatura": str(item.get("thumbnail") or ""),
                "resumo": str(item.get("snippet") or item.get("description") or ""),
                "data_resultado": str(item.get("date") or item.get("date_crawled") or ""),
                "related_content_id": str(item.get("related_content_id") or ""),
            }
            if resultado["miniatura"] and not validar_url_imagem(resultado["miniatura"]):
                resultado["miniatura"] = ""
            resultados.append(resultado)
    return resultados


def normalizar_perfil_instagram(dados: dict) -> tuple[dict, list[dict[str, str]]]:
    perfil_original = dados.get("profile_results")
    if not isinstance(perfil_original, dict):
        return {}, []

    username = str(perfil_original.get("username") or "")
    perfil = {
        "username": username,
        "nome": str(perfil_original.get("full_name") or ""),
        "biografia": str(perfil_original.get("biography") or ""),
        "seguidores": perfil_original.get("followers", "—"),
        "seguindo": perfil_original.get("following", "—"),
        "privado": bool(perfil_original.get("is_private")),
        "verificado": bool(perfil_original.get("is_verified")),
        "link": f"https://www.instagram.com/{username}/"
        if re.fullmatch(r"[A-Za-z0-9._]{1,30}", username)
        else "",
        "foto_perfil": str(
            perfil_original.get("profile_image_url")
            or perfil_original.get("profile_pic_url")
            or perfil_original.get("picture")
            or ""
        ),
    }

    posts = []
    posts_originais = perfil_original.get("posts", [])
    if not isinstance(posts_originais, list):
        posts_originais = []
    for post in posts_originais:
        if not isinstance(post, dict):
            continue
        shortcode = str(post.get("shortcode") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", shortcode):
            continue
        captions = post.get("media_captions")
        if not isinstance(captions, list):
            captions = []
        caption = captions[0] if captions else post.get("accessibility_caption", "")
        posts.append(
            {
                "titulo": str(caption or "Publicação do perfil"),
                "link": f"https://www.instagram.com/p/{shortcode}/",
                "tipo": str(post.get("product_type") or ""),
                "miniatura": next(
                    (
                        post[chave]
                        for chave in ("thumbnail", "thumbnail_src", "display_url", "image")
                        if isinstance(post.get(chave), str)
                        and post[chave].startswith("https://")
                    ),
                    "",
                ),
                # Proxy da SerpApi: usado quando a CDN do Instagram recusa o download
                "miniatura_alternativa": next(
                    (
                        post[chave]
                        for chave in ("serpapi_thumbnail_src", "serpapi_display_url")
                        if isinstance(post.get(chave), str)
                        and post[chave].startswith("https://")
                    ),
                    "",
                ),
            }
        )
    return perfil, posts


def normalizar_instagram_popular(dados: dict) -> list[dict[str, str]]:
    posts = []
    resultados = dados.get("popular_results", [])
    if not isinstance(resultados, list):
        return posts
    for post in resultados:
        if not isinstance(post, dict):
            continue
        link = post.get("link")
        if not isinstance(link, str) or not validar_url_imagem(link):
            continue
        usuario = post.get("user")
        usuario = usuario if isinstance(usuario, dict) else {}
        link_perfil = usuario.get("link")
        posts.append(
            {
                "titulo": str(post.get("caption") or "Publicação popular"),
                "link": link,
                "usuario": str(usuario.get("username") or ""),
                "link_perfil": link_perfil
                if isinstance(link_perfil, str) and validar_url_imagem(link_perfil)
                else "",
                "reproducoes": str(post.get("play_count") or ""),
            }
        )
    return posts
