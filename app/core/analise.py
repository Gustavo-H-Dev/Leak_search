from collections import Counter
from urllib.parse import urlsplit

from app.core import candidatos, imagens, sites_oficiais

STATUS = ("pending", "leaked", "not_me", "official")


def _dominio(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _nivel(candidato, referencias):
    melhor = candidatos.melhor_referencia(candidato["phash"], referencias)
    if sites_oficiais.pagina_de_listagem(candidato["url"]):
        return "diferente", None
    if melhor is None:
        return "sem_referencia", None
    return imagens.nivel_de_similaridade(melhor["distancia"]), melhor["distancia"]


def analisar_usuario(lista, referencias, hashes_oficiais_total):
    """Resume os candidatos de uma criadora e aponta pontos de atenção."""
    por_status = Counter(c["status"] for c in lista)
    sem_hash = sum(1 for c in lista if not c["phash"])
    niveis = {s: Counter() for s in STATUS}
    pontuacoes = {s: [] for s in STATUS}
    dominios = {s: Counter() for s in STATUS}
    motores = Counter(c["engine"] for c in lista)
    for c in lista:
        nivel, distancia = _nivel(c, referencias)
        niveis[c["status"]][nivel] += 1
        if distancia is not None:
            pontuacoes[c["status"]].append(imagens.pontuacao(distancia))
        dominios[c["status"]][_dominio(c["url"])] += 1

    decididos_vazado = por_status["leaked"]
    decididos_nao_eu = por_status["not_me"]
    alertas = []
    if not referencias:
        alertas.append(
            "Sem imagens de referência: nada pode ser comparado. Cadastre catálogo, "
            "fotos do perfil ou importe os posts oficiais do Instagram."
        )
    if lista and sem_hash / len(lista) > 0.3:
        alertas.append(
            f"{sem_hash} de {len(lista)} candidatos ficaram sem hash (miniatura "
            "indisponível); eles caem em Diferentes sem terem sido comparados."
        )
    perdidos = niveis["leaked"]["diferente"] + niveis["leaked"]["sem_referencia"]
    if decididos_vazado >= 3 and perdidos / decididos_vazado > 0.3:
        alertas.append(
            "Muitos vazados confirmados estão em Diferentes: os limites de "
            "semelhança estão rígidos demais ou faltam referências (maquiagem, "
            "ângulos, vídeos)."
        )
    if decididos_nao_eu >= 3 and niveis["not_me"]["atencao"] / decididos_nao_eu > 0.3:
        alertas.append(
            "Muitos 'não sou eu' estão em Atenção: os limites estão frouxos demais."
        )
    pendentes = por_status["pending"]
    if pendentes >= 50:
        alertas.append(f"{pendentes} candidatos pendentes aguardando validação.")
    if not decididos_vazado and not decididos_nao_eu and lista:
        alertas.append(
            "Ainda não há candidatos validados; não dá para calibrar os limites."
        )

    def media(valores):
        return round(sum(valores) / len(valores)) if valores else None

    return {
        "total": len(lista),
        "por_status": {s: por_status[s] for s in STATUS},
        "sem_hash": sem_hash,
        "niveis": {s: dict(niveis[s]) for s in STATUS},
        "media_pontuacao": {s: media(pontuacoes[s]) for s in STATUS},
        "dominios": {s: dominios[s].most_common(5) for s in ("leaked", "pending")},
        "motores": motores.most_common(),
        "referencias": len(referencias),
        "hashes_oficiais": hashes_oficiais_total,
        "alertas": alertas,
    }
