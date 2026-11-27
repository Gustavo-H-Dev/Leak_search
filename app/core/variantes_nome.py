"""Variantes do arroba/nome usadas nas consultas e na pontuacao.

Um arroba como "morph1ne2.0" quase nunca aparece igual no titulo de um album:
o site usa "morph1ne". As variantes cobrem esse caso sem precisar de cadastro
manual e sem baixar midia nenhuma.
"""

import re

MINIMO_CARACTERES = 5
_FIM_DIGITOS_PONTUACAO = re.compile(r"[0-9._\-]+$")
_LETRAS_REPETIDAS = re.compile(r"(.)\1{2,}")


def _normalizar(arroba: str) -> str:
    return (arroba or "").strip().lstrip("@").lower()


def _sem_fim(nome: str) -> str:
    """Remove digitos e pontuacao no FIM do nome (mantem os do meio)."""
    return _FIM_DIGITOS_PONTUACAO.sub("", nome)


def _sem_repeticao(nome: str) -> str:
    """Troca 3 ou mais letras iguais seguidas por uma so."""
    return _LETRAS_REPETIDAS.sub(r"\1", nome)


def gerar_variantes_nome(arroba: str, incluir_curtas: bool = False) -> list[str]:
    """Variantes de um arroba, em minusculas e sem "@", sem duplicatas.

    Inclui o original, a forma sem "." e "_", a forma sem digitos/pontuacao no
    fim, a forma com letras repetidas reduzidas e as combinacoes dessas duas
    ultimas. Variantes com menos de 5 caracteres so entram com
    `incluir_curtas=True`, pois so funcionam junto de um termo de hospedagem.
    """
    base = _normalizar(arroba)
    if not base:
        return []

    origens = [base]
    sem_pontos = re.sub(r"[._]", "", base)
    if sem_pontos != base:
        origens.append(sem_pontos)

    tentativas = []
    for nome in origens:
        tentativas.extend(
            (
                nome,
                _sem_fim(nome),
                _sem_repeticao(nome),
                _sem_repeticao(_sem_fim(nome)),
                _sem_fim(_sem_repeticao(nome)),
            )
        )

    variantes: list[str] = []
    for nome in tentativas:
        nome = nome.strip("._-")
        if not nome or nome in variantes:
            continue
        if not incluir_curtas and len(nome) < MINIMO_CARACTERES:
            continue
        variantes.append(nome)
    return variantes


def nome_base(arrobas) -> str:
    """Variante mais comum entre os arrobas; empate resolve pela mais curta.

    E o nome que aparece nos titulos, como "morph1ne". Devolve "" quando
    nenhum arroba tem variante utilizavel.
    """
    contagem: dict[str, int] = {}
    for arroba in arrobas or ():
        for variante in gerar_variantes_nome(arroba):
            contagem[variante] = contagem.get(variante, 0) + 1
    if not contagem:
        return ""
    return min(contagem, key=lambda nome: (-contagem[nome], len(nome), nome))


def variantes_dos_arrobas(arrobas) -> list[str]:
    """Uniao das variantes de todos os arrobas, sem duplicatas."""
    variantes: list[str] = []
    for arroba in arrobas or ():
        for variante in gerar_variantes_nome(arroba):
            if variante not in variantes:
                variantes.append(variante)
    return variantes
