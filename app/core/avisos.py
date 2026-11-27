"""Avisos por e-mail: texto sem conteudo sensivel e envio por SMTP.

O e-mail nunca leva trecho, miniatura ou URL do site: so a contagem de novos,
o dominio e o titulo dos tres de maior nota, e o link da fila. O link exige
login e nao carrega dados na URL.

O transporte e SMTP puro, entao serve para Resend, Postmark, Mailgun, Amazon
SES, SendGrid ou um servidor proprio: basta preencher SMTP_HOST, SMTP_PORT,
SMTP_USER, SMTP_PASSWORD e SMTP_FROM.
"""

import html
import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from app import db
from app.core import pontuacao

logger = logging.getLogger(__name__)
TIPOS = {
    "novos": "Novos resultados",
    "resumo_diario": "Resumo diário",
    "resumo_semanal": "Resumo semanal",
}


def configuracao_smtp() -> dict:
    try:
        porta = int(os.getenv("SMTP_PORT", "587") or 587)
    except ValueError:
        porta = 587
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "porta": porta,
        "usuario": os.getenv("SMTP_USER", "").strip(),
        "senha": os.getenv("SMTP_PASSWORD", ""),
        "remetente": os.getenv("SMTP_FROM", "").strip(),
        "tls": os.getenv("SMTP_TLS", "1").strip().lower() not in {"0", "false", "nao"},
        "ssl": (
            os.getenv("SMTP_SSL", "").strip().lower() in {"1", "true", "sim"}
            or os.getenv("SMTP_USE_SSL", "").strip().lower() in {"1", "true", "sim"}
        ),
    }


def configurado() -> bool:
    dados = configuracao_smtp()
    return bool(dados["host"] and dados["remetente"])


def url_da_fila() -> str:
    base = os.getenv("APP_BASE_URL", "http://localhost:3000").strip().rstrip("/")
    return f"{base}/revisao"


def _nome(usuario) -> str:
    return (usuario["public_name"] or usuario["username"] or "").strip()


def _titulo_curto(texto, limite=70) -> str:
    limpo = " ".join((texto or "").split())
    if len(limpo) <= limite:
        return limpo or "(sem título)"
    return limpo[: limite - 1].rstrip() + "…"


def _linhas_top(top) -> list[str]:
    return [
        f"- {item['dominio']} — {_titulo_curto(item['title'])}"
        for item in top
    ]


def _texto_base(nome, total, top, link, periodo) -> str:
    linhas = _linhas_top(top)
    return (
        f"Olá, {nome}.\n\n"
        f"{total} novo(s) resultado(s) {periodo} para revisar.\n\n"
        "Os 3 de maior nota (só site e título):\n"
        + ("\n".join(linhas) if linhas else "- nenhum")
        + f"\n\nAbrir a fila de revisão: {link}\n"
        "\nO link pede login. Nenhum trecho, imagem ou endereço do site vai "
        "neste e-mail.\n"
        "\nVocê recebe este aviso porque ativou as notificações em "
        "Configurações. Dá para desligar a qualquer momento.\n"
    )


def _html_base(nome, total, top, link, periodo) -> str:
    itens = "".join(
        "<li><strong>{}</strong> — {}</li>".format(
            html.escape(item["dominio"] or ""), html.escape(_titulo_curto(item["title"]))
        )
        for item in top
    ) or "<li>nenhum</li>"
    return f"""<!doctype html>
<html lang="pt-br"><body style="font-family:Arial,sans-serif;color:#1a1a1a">
<p>Olá, {html.escape(nome)}.</p>
<p><strong>{total} novo(s) resultado(s)</strong> {html.escape(periodo)} para revisar.</p>
<p>Os 3 de maior nota (só site e título):</p>
<ul>{itens}</ul>
<p><a href="{html.escape(link)}">Abrir a fila de revisão</a></p>
<p style="color:#555;font-size:12px">
O link pede login e não carrega dados na URL. Nenhum trecho, imagem ou
endereço do site vai neste e-mail. Você recebe este aviso porque ativou as
notificações em Configurações; dá para desligar a qualquer momento.
</p>
</body></html>"""


def montar_email_novos(usuario, total, top):
    assunto = f"{total} novo(s) resultado(s) para revisar"
    link = url_da_fila()
    nome = _nome(usuario)
    return (
        assunto,
        _texto_base(nome, total, top, link, "depois da última varredura"),
        _html_base(nome, total, top, link, "depois da última varredura"),
    )


def montar_email_resumo(usuario, tipo, total, top):
    periodo = "hoje" if tipo == "resumo_diario" else "nesta semana"
    assunto = f"Resumo {'diário' if tipo == 'resumo_diario' else 'semanal'}: {total} novo(s)"
    link = url_da_fila()
    nome = _nome(usuario)
    return assunto, _texto_base(nome, total, top, link, periodo), _html_base(
        nome, total, top, link, periodo
    )


def enviar_email(destino, assunto, texto, corpo_html) -> None:
    dados = configuracao_smtp()
    if not (dados["host"] and dados["remetente"]):
        raise RuntimeError("SMTP não configurado (SMTP_HOST/SMTP_FROM).")
    mensagem = EmailMessage()
    mensagem["Subject"] = assunto
    mensagem["From"] = dados["remetente"]
    mensagem["To"] = destino
    mensagem.set_content(texto)
    mensagem.add_alternative(corpo_html, subtype="html")
    # Porta 465 (SSL direto) é comum em provedores como Brevo e Mailgun.
    usar_ssl = dados["ssl"] or dados["porta"] == 465
    classe = smtplib.SMTP_SSL if usar_ssl else smtplib.SMTP
    with classe(dados["host"], dados["porta"], timeout=20) as conexao:
        if dados["tls"] and not usar_ssl:
            conexao.starttls()
        if dados["usuario"]:
            conexao.login(dados["usuario"], dados["senha"])
        conexao.send_message(mensagem)


def notificar_novos(criadora_id, execucao_id):
    """Cria o aviso da varredura quando a preferência está ligada."""
    preferencias = db.obter_preferencia_aviso(criadora_id)
    if not int(preferencias.get("email_scan") or 0):
        return None
    usuario = db.obter_usuario_por_id(criadora_id)
    if usuario is None or not (usuario["email"] or "").strip():
        return None
    candidatos = db.candidatos_novos_da_execucao(
        execucao_id, pontuacao.limiar_revisao()
    )
    if not candidatos:
        return None
    assunto, texto, corpo_html = montar_email_novos(
        usuario, len(candidatos), candidatos[:3]
    )
    aviso_id = db.criar_aviso(
        criadora_id, "novos", f"novos:{execucao_id}", assunto, texto, corpo_html
    )
    if aviso_id:
        enviar_pendentes()
    return aviso_id


def _quando(iso):
    try:
        momento = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento


def processar_resumos(base=None):
    """Enfileira resumos diário/semanal devidos, sem repetir o mesmo período."""
    base = base or datetime.now(timezone.utc)
    limiar = pontuacao.limiar_revisao()
    criados = []
    for usuario in db.listar_usuarios():
        if usuario["role"] == "admin":
            continue
        preferencias = db.obter_preferencia_aviso(usuario["id"])
        for tipo, chave_pref, dias in (
            ("resumo_diario", "resumo_diario", 1),
            ("resumo_semanal", "resumo_semanal", 7),
        ):
            if not int(preferencias.get(chave_pref) or 0):
                continue
            ultimo = _quando(preferencias.get(f"ultimo_{chave_pref}"))
            if ultimo is not None:
                if tipo == "resumo_diario" and ultimo.date() >= base.date():
                    continue
                if tipo == "resumo_semanal" and (base - ultimo) < timedelta(days=7):
                    continue
            desde = ultimo or (base - timedelta(days=dias))
            total = db.contar_novos_desde(usuario["id"], desde.isoformat(), limiar)
            if not total:
                continue
            top = db.top_candidatos_desde(usuario["id"], desde.isoformat(), limiar)
            assunto, texto, corpo_html = montar_email_resumo(usuario, tipo, total, top)
            chave = f"{tipo}:{usuario['id']}:{base.date().isoformat()}"
            if db.criar_aviso(usuario["id"], tipo, chave, assunto, texto, corpo_html):
                db.registrar_resumo_enviado(usuario["id"], tipo, base.isoformat())
                criados.append(usuario["id"])
    if criados:
        enviar_pendentes()
    return criados


def enviar_pendentes(limite=20):
    """Envia a caixa de saída. Sem SMTP configurado, nada é descartado."""
    if not configurado():
        return {"enviados": 0, "motivo": "SMTP não configurado."}
    enviados = 0
    for aviso in db.listar_avisos_pendentes(limite):
        if not (aviso["email"] or "").strip() or not int(aviso["email_verified"] or 0):
            db.marcar_aviso_erro(aviso["id"], "E-mail da conta não confirmado.")
            continue
        try:
            enviar_email(
                aviso["email"], aviso["assunto"], aviso["corpo_texto"], aviso["corpo_html"]
            )
            db.marcar_aviso_enviado(aviso["id"])
            enviados += 1
        except Exception as falha:  # SMTP fora do ar não derruba a busca
            logger.warning("Falha ao enviar aviso %s: %s", aviso["id"], falha)
            db.marcar_aviso_erro(aviso["id"], str(falha))
    return {"enviados": enviados, "motivo": ""}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    db.iniciar_banco()
    print(processar_resumos())
    print(enviar_pendentes())


if __name__ == "__main__":
    main()
