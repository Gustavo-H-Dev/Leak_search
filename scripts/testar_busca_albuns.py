"""Teste manual (fora do CI) da busca de albuns com a chave real do TinyFish.

Uso:
    TINYFISH_API_KEY=... PYTHONPATH=. python scripts/testar_busca_albuns.py

Confere as duas consultas do caso real (M1 com os arrobas e M2 com o nome-base
+ bunkr), roda cada modelo no provedor e registra tudo em
`data/teste_busca_albuns.json` para comparar execucoes ao longo do tempo.

Nada de midia e baixado: so titulo, URL, trecho e a resposta bruta da busca.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import (  # noqa: E402
    busca_provedores,
    consultas_vazamento,
    pontuacao,
    variantes_nome,
)

ARROBAS = ["Morph1ne2.0", "Morph1neee_"]
CONSULTAS_ESPERADAS = [
    "Morph1ne2.0 OR Morph1neee_ vazado",
    "morph1ne videos bunkr",
]
ID_ALBUM = "7W7gu1yU"
DESTINO = Path("data/teste_busca_albuns.json")


def main() -> int:
    if not os.getenv("TINYFISH_API_KEY", "").strip():
        print("Defina TINYFISH_API_KEY para rodar o teste real do TinyFish.")
        return 2

    limite = int(os.getenv("LIMITE_CONSULTAS", "10"))
    planos = consultas_vazamento.gerar_consultas_priorizadas(
        ARROBAS, ["vazado"], [], limite
    )
    consultas = [plano["consulta"] for plano in planos]
    faltando = [consulta for consulta in CONSULTAS_ESPERADAS if consulta not in consultas]
    variantes = variantes_nome.variantes_dos_arrobas(ARROBAS)
    base = variantes_nome.nome_base(ARROBAS)

    print(f"Variantes: {variantes}")
    print(f"Nome-base: {base}")
    print(f"Consultas geradas ({len(planos)}):")
    for plano in planos:
        print(f"  {plano['modelo']} · {plano['consulta']}")
    if faltando:
        print(f"FALTANDO as consultas esperadas: {faltando}")
    else:
        print("As duas consultas do caso real estao na lista.")

    registro = {
        "executado_em": datetime.now(timezone.utc).isoformat(),
        "arrobas": ARROBAS,
        "variantes": variantes,
        "nome_base": base,
        "consultas_esperadas": CONSULTAS_ESPERADAS,
        "consultas_faltando": faltando,
        "execucoes": [],
    }
    achou_album = False
    for plano in planos:
        try:
            candidatos = busca_provedores.buscar("tinyfish", plano["consulta"])
        except Exception as erro:  # noqa: BLE001 - o log precisa do texto
            print(f"[{plano['modelo']}] {plano['consulta']}: erro {erro}")
            registro["execucoes"].append(
                {"modelo": plano["modelo"], "consulta": plano["consulta"], "erro": str(erro)}
            )
            continue
        itens = []
        for posicao, candidato in enumerate(candidatos, start=1):
            avaliacao = pontuacao.avaliar(
                candidato.url,
                candidato.titulo,
                candidato.trecho,
                arrobas=ARROBAS,
                variantes=variantes,
                nome_base=base,
            )
            chave = pontuacao.chave_grupo(candidato.url)
            if ID_ALBUM in candidato.url:
                achou_album = True
            itens.append(
                {
                    "posicao": posicao,
                    "url": candidato.url,
                    "titulo": candidato.titulo,
                    "trecho": candidato.trecho,
                    "nota": avaliacao["nota"],
                    "classificacao": avaliacao["classificacao"],
                    "motivos": avaliacao["motivos"],
                    "chave_grupo": chave,
                }
            )
            marca = " <== ALBUM" if ID_ALBUM in candidato.url else ""
            print(
                f"  {posicao}. [{avaliacao['nota']}] {candidato.url} — "
                f"{candidato.titulo}{marca}"
            )
        registro["execucoes"].append(
            {
                "modelo": plano["modelo"],
                "consulta": plano["consulta"],
                "parametros": plano["parametros"],
                "resultados": itens,
            }
        )

    registro["album_encontrado"] = achou_album
    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text(
        json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nAlbum {ID_ALBUM} encontrado: {'sim' if achou_album else 'nao'}")
    print(f"Registro salvo em {DESTINO}")
    if faltando or not achou_album:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
