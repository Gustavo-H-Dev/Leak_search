"""Agenda de buscas por criadora e disparo da fila.

Regras sem I/O de rede: o horario escolhido e o do fuso de Sao Paulo (o app
inteiro e brasileiro), e o banco guarda sempre UTC. O scheduler so pede os
vencidos; a reserva atomica no banco garante um unico disparo por horario.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import db
from app.core import busca_provedores

FUSO = ZoneInfo("America/Sao_Paulo")
FREQUENCIAS = {"semanal": "Toda semana", "diario": "Todo dia"}
DIAS_SEMANA = (
    (0, "Segunda-feira"),
    (1, "Terça-feira"),
    (2, "Quarta-feira"),
    (3, "Quinta-feira"),
    (4, "Sexta-feira"),
    (5, "Sábado"),
    (6, "Domingo"),
)
HORAS = tuple(range(0, 24))
PADRAO = {"frequencia": "semanal", "hora": 9, "dia_semana": 0}
MOTIVOS_FALHA = {
    "sem_creditos": "Provedor sem créditos (402)",
    "limite_provedor": "Limite do provedor atingido (429)",
    "instagram_bloqueado": "Instagram bloqueou a consulta",
    "configuracao": "Configuração incompleta",
    "interrompida": "Execução interrompida",
    "fila": "Falha na fila",
    "erro": "Falha na execução",
    "": "",
}
STATUS_TEXTOS = {
    "na_fila": "Na fila",
    "rodando": "Rodando",
    "concluida": "Concluída",
    "avisos": "Concluída com avisos",
    "erro": "Falhou",
    "pendente": "Pendente",
}


def agora(fuso: ZoneInfo = FUSO) -> datetime:
    return datetime.now(timezone.utc).astimezone(fuso)


def em_utc(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).isoformat()


def inicio_do_dia(fuso: ZoneInfo = FUSO) -> datetime:
    return agora(fuso).replace(hour=0, minute=0, second=0, microsecond=0)


def validar(frequencia: str, hora: int, dia_semana: int) -> tuple[str, int, int]:
    frequencia = (frequencia or "").strip().lower()
    if frequencia not in FREQUENCIAS:
        raise ValueError("Escolha entre busca diária e semanal.")
    try:
        hora = int(hora)
        dia_semana = int(dia_semana)
    except (TypeError, ValueError):
        raise ValueError("Informe um horário válido.") from None
    if hora not in HORAS:
        raise ValueError("Escolha uma hora entre 0 e 23.")
    if dia_semana not in range(7):
        raise ValueError("Escolha um dia da semana válido.")
    return frequencia, hora, dia_semana


def proxima_execucao(
    frequencia: str,
    hora: int,
    dia_semana: int = 0,
    *,
    base: datetime | None = None,
) -> datetime:
    """Proximo horario (no fuso local) em que a busca deve rodar."""
    base = (base or agora()).astimezone(FUSO)
    candidato = base.replace(hour=int(hora), minute=0, second=0, microsecond=0)
    if (frequencia or "").strip().lower() == "diario":
        if candidato <= base:
            candidato += timedelta(days=1)
        return candidato
    avanco = (int(dia_semana) - base.weekday()) % 7
    candidato += timedelta(days=avanco)
    if candidato <= base:
        candidato += timedelta(days=7)
    return candidato


def salvar(criadora_id: int, frequencia: str, hora: int, dia_semana: int) -> dict:
    frequencia, hora, dia_semana = validar(frequencia, hora, dia_semana)
    proxima = proxima_execucao(frequencia, hora, dia_semana)
    db.salvar_agendamento_busca(
        criadora_id, frequencia, hora, dia_semana, em_utc(proxima), ativo=1
    )
    return descricao(db.obter_agendamento_busca(criadora_id))


def desativar(criadora_id: int) -> bool:
    return db.desativar_agendamento_busca(criadora_id)


def _hora_texto(hora: int) -> str:
    return f"{int(hora):02d}:00"


def descricao(registro: dict | None) -> dict:
    """Dados prontos para a tela, com a proxima execucao no fuso local."""
    if not registro:
        return {
            "configurado": False,
            "ativo": False,
            "frequencia": PADRAO["frequencia"],
            "hora": PADRAO["hora"],
            "dia_semana": PADRAO["dia_semana"],
            "resumo": "Sem agendamento. O padrão é semanal.",
            "proxima": "",
        }
    dia = dict(DIAS_SEMANA).get(int(registro["dia_semana"]), "")
    if registro["frequencia"] == "diario":
        resumo = f"Todos os dias às {_hora_texto(registro['hora'])}"
    else:
        resumo = f"{dia} às {_hora_texto(registro['hora'])}"
    proxima = ""
    if registro.get("proxima_em"):
        try:
            momento = datetime.fromisoformat(registro["proxima_em"]).astimezone(FUSO)
            proxima = momento.strftime("%d/%m/%Y %H:%M")
        except (TypeError, ValueError):
            proxima = ""
    return {
        "configurado": True,
        "ativo": bool(int(registro.get("ativo") or 0)),
        "frequencia": registro["frequencia"],
        "hora": int(registro["hora"]),
        "dia_semana": int(registro["dia_semana"]),
        "id": registro.get("id"),
        "resumo": resumo,
        "proxima": proxima,
    }


def provedor_padrao() -> str:
    for nome in ("serpapi", "tinyfish"):
        if busca_provedores.configurado(nome):
            return nome
    return ""


def pode_disparar_agora(criadora_id: int) -> tuple[bool, str]:
    em_andamento = db.execucao_em_andamento(criadora_id)
    if em_andamento:
        return (
            False,
            "Já existe uma busca na fila ou rodando para esta conta. "
            "Aguarde ela terminar.",
        )
    usadas = db.contar_execucoes_desde(criadora_id, em_utc(inicio_do_dia()))
    limite = db.limite_buscas_por_dia()
    if usadas >= limite:
        return (
            False,
            f"Limite de {limite} busca(s) por dia atingido. "
            "A próxima janela abre à meia-noite (horário de Brasília).",
        )
    if not provedor_padrao():
        return (
            False,
            "Configure SERPAPI_API_KEY ou TINYFISH_API_KEY no servidor "
            "para rodar a busca.",
        )
    return True, ""


def disparar_agora(
    criadora_id: int, *, origem: str = "manual", agendamento_id=None
) -> tuple[int | None, str]:
    """Valida limites, cria a execucao na fila e tenta enfileirar no RQ."""
    ok, motivo = pode_disparar_agora(criadora_id)
    if not ok:
        return None, motivo
    provedor = provedor_padrao()
    execucao_id = db.criar_busca_execucao(
        criadora_id,
        provedor,
        [],
        status="na_fila",
        origem=origem,
        agendamento_id=agendamento_id,
    )
    # Import tardio: o app web nao precisa de Redis para subir.
    from app.core import fila_buscas

    envio = fila_buscas.enfileirar(criadora_id, execucao_id, provedor)
    if not envio["ok"]:
        db.finalizar_busca_execucao(
            execucao_id,
            status="erro",
            motivo_falha="fila",
            erro=envio["mensagem"],
        )
        return None, envio["mensagem"]
    return execucao_id, "Busca na fila. O resultado aparece no histórico."


def processar_vencidos(limite: int = 20, base: datetime | None = None) -> list[dict]:
    """Reserva e enfileira os agendamentos vencidos. Um disparo por horario."""
    base = base or agora()
    resultados = []
    for item in db.listar_agendamentos_vencidos(em_utc(base), limite):
        proxima = proxima_execucao(
            item["frequencia"],
            item["hora"],
            item["dia_semana"],
            base=base,
        )
        reservado = db.reservar_agendamento_busca(
            item["agendamento_id"], item["proxima_em"], em_utc(proxima)
        )
        if not reservado:
            # Outro processo/scheduler ja pegou este horario.
            continue
        execucao_id, mensagem = disparar_agora(
            item["criadora_id"], origem="agendada", agendamento_id=item["agendamento_id"]
        )
        resultados.append(
            {
                "criadora_id": item["criadora_id"],
                "agendamento_id": item["agendamento_id"],
                "execucao_id": execucao_id,
                "mensagem": mensagem,
                "proxima_em": em_utc(proxima),
            }
        )
    return resultados


def estado(criadora_id: int) -> dict:
    registro = db.obter_agendamento_busca(criadora_id)
    usadas = db.contar_execucoes_desde(criadora_id, em_utc(inicio_do_dia()))
    return {
        **descricao(registro),
        "limite_dia": db.limite_buscas_por_dia(),
        "usadas_hoje": usadas,
        "em_andamento": db.execucao_em_andamento(criadora_id),
        "ultima": db.ultima_execucao(criadora_id),
        "frequencias": FREQUENCIAS,
        "dias_semana": DIAS_SEMANA,
        "horas": HORAS,
        "provedor": provedor_padrao(),
    }
