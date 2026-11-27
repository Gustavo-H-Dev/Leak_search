from app.core import consultas_vazamento as cv


def test_limpa_termos_e_sites():
    assert cv.limpar_termos('vazado, "Leaked"\nvazado\n(naked)') == ["vazado", "Leaked", "naked"]
    assert cv.limpar_sites("https://www.erome.com/x\nfoo bar\nexemplo.com") == ["erome.com", "exemplo.com"]


def test_gera_consultas_por_arroba_com_limite():
    consultas = cv.gerar_consultas_busca(
        ["ana", "aninha"], ["vazado", "leaked"], ["erome.com"], 4
    )
    assert len(consultas) == 4
    assert consultas[0] == '"@ana" "vazado"'
    assert consultas[1] == '"@aninha" "vazado"'

    com_site = cv.gerar_consultas_busca(["ana"], ["vazado"], ["erome.com"], 5)
    assert com_site[-1] == '"@ana" site:erome.com'


def test_agrupa_em_poucas_consultas():
    sites = [f"s{i}.com" for i in range(30)]
    consultas = cv.montar_consultas("Ana", "@ana", ["vazado", "nova foto"], sites)
    assert len(consultas) == 3
    assert '"nova foto"' in consultas[0] and "tbs" not in consultas[0]
    assert consultas[1].count("site:") == 5
