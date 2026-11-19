import hashlib
import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from email_validator import EmailNotValidError, validate_email

password_hasher = PasswordHasher()
MINIMO_SENHA = 12
MAXIMO_SENHA = 128


class ErroEmail(Exception):
    pass


def validar_usuario(username: str) -> str:
    username = username.strip()
    if not 3 <= len(username) <= 32:
        raise ValueError("O usuário deve ter entre 3 e 32 caracteres.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", username):
        raise ValueError("Use apenas letras, números, ponto, hífen ou sublinhado.")
    return username


def validar_email(email: str) -> str:
    try:
        return validate_email(email.strip(), check_deliverability=False).normalized
    except EmailNotValidError as erro:
        raise ValueError("Informe um endereço de e-mail válido.") from erro


def validar_senha(senha: str) -> str:
    if len(senha) < MINIMO_SENHA:
        raise ValueError(f"A senha deve ter pelo menos {MINIMO_SENHA} caracteres.")
    if len(senha) > MAXIMO_SENHA:
        raise ValueError(f"A senha deve ter no máximo {MAXIMO_SENHA} caracteres.")
    return senha


def hash_senha(senha: str) -> str:
    validar_senha(senha)
    return password_hasher.hash(senha)


def verificar_senha(password_hash: str, senha: str) -> bool:
    try:
        return password_hasher.verify(password_hash, senha)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


def gerar_token(expiracao: timedelta) -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    vence_em = (datetime.now(timezone.utc) + expiracao).isoformat()
    return token, token_hash, vence_em


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def email_configurado() -> bool:
    return bool(
        os.getenv("SMTP_HOST", "").strip()
        and os.getenv("SMTP_USER", "").strip()
        and os.getenv("SMTP_PASSWORD", "").strip()
    )


def confirmacao_email_obrigatoria() -> bool:
    return os.getenv("EMAIL_VERIFICATION_REQUIRED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def enviar_email(destinatario: str, assunto: str, corpo: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    usuario = os.getenv("SMTP_USER", "").strip()
    senha = os.getenv("SMTP_PASSWORD", "")
    remetente = os.getenv("SMTP_FROM", usuario).strip()
    try:
        porta = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as erro:
        raise ErroEmail("A porta SMTP configurada não é válida.") from erro

    if not host or not usuario or not senha or not remetente:
        raise ErroEmail("O envio de e-mail ainda não está configurado no servidor.")

    mensagem = EmailMessage()
    mensagem["Subject"] = assunto
    mensagem["From"] = remetente
    mensagem["To"] = destinatario
    mensagem.set_content(corpo)
    contexto = ssl.create_default_context()

    try:
        if os.getenv("SMTP_USE_SSL", "").lower() in {"1", "true", "yes"}:
            with smtplib.SMTP_SSL(host, porta, context=contexto, timeout=20) as servidor:
                servidor.login(usuario, senha)
                recusados = servidor.send_message(mensagem)
        else:
            with smtplib.SMTP(host, porta, timeout=20) as servidor:
                servidor.ehlo()
                servidor.starttls(context=contexto)
                servidor.ehlo()
                servidor.login(usuario, senha)
                recusados = servidor.send_message(mensagem)
    except (OSError, smtplib.SMTPException) as erro:
        raise ErroEmail("Não foi possível enviar o e-mail. Confira as configurações SMTP.") from erro
    if recusados:
        raise ErroEmail("O servidor SMTP recusou o destinatário.")
