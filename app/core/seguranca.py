"""Cabecalhos de seguranca e IP do cliente.

O IP vem da conexao TCP (request.client), nunca do X-Forwarded-For: um cabecalho
inventado nao pode furar o limite de tentativas de login. Atras de proxy, o
limite por usuario continua valendo; o por IP passa a ser o do proxy.
"""

import os

from starlette.middleware.base import BaseHTTPMiddleware


def ip_do_cliente(request) -> str:
    cliente = getattr(request, "client", None)
    return (cliente.host if cliente else "") or ""


class CabecalhosSeguranca(BaseHTTPMiddleware):
    """CSP, anti-clickjacking, nosniff e HSTS sem quebrar o app atual."""

    async def dispatch(self, request, call_next):
        resposta = await call_next(request)
        cabecalhos = resposta.headers
        cabecalhos.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )
        cabecalhos.setdefault("X-Content-Type-Options", "nosniff")
        cabecalhos.setdefault("X-Frame-Options", "DENY")
        cabecalhos.setdefault("Referrer-Policy", "same-origin")
        cabecalhos.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        cabecalhos.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if os.getenv("COOKIE_SECURE", "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            cabecalhos.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        caminho = request.url.path
        if not caminho.startswith("/static/") and "text/html" in cabecalhos.get(
            "content-type", ""
        ):
            cabecalhos.setdefault("Cache-Control", "no-store")
        return resposta
