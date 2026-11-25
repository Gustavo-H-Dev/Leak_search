import hashlib
import secrets
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app import db


ROTAS_PUBLICAS = {
    "/",
    "/login",
    "/cadastro",
    "/confirmar-email",
    "/reenviar-confirmacao",
    "/esqueci-senha",
    "/redefinir-senha",
    "/verificar-instagram",
    "/debug-instagram",
    "/test-instagram",
}


class AutenticacaoMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
        token_sessao = request.session.get("auth_session")
        usuario = None
        if isinstance(token_sessao, str):
            token_hash = hashlib.sha256(token_sessao.encode("utf-8")).hexdigest()
            usuario = db.obter_usuario_da_sessao(token_hash)
            if usuario is None:
                db.revogar_sessao(token_hash)
                request.session.clear()
                request.session["csrf_token"] = secrets.token_urlsafe(32)

        request.state.user = usuario
        if request.url.path not in ROTAS_PUBLICAS and usuario is None:
            destino = quote(request.url.path)
            return RedirectResponse(f"/login?next={destino}", status_code=303)

        return await call_next(request)
