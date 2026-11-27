from app.core import consultas_vazamento as cv

ARROBAS = ["Morph1ne2.0", "Morph1neee_"]


def test_limpa_termos_e_sites():
    assert cv.limpar_termos('vazado, "Leaked"\nvazado\n(naked)') == ["vazado", "Leaked", "naked"]
    assert cv.limpar_sites("https://www.erome.com/x\nfoo bar\nexemplo.com") == ["erome.com", "exemplo.com"]


def test_modelo_m1_junta_arrobas_com_or():
    planos = cv.gerar_consultas_priorizadas(ARROBAS, ["vazado"], [], 18)
    assert planos[0]["modelo"] == "M1"
    assert planos[0]["consulta"] == "Morph1ne2.0 OR Morph1neee_ vazado"
    assert planos[0]["parametros"]["arrobas"] == ARROBAS
    # O nome-base em minusculas continua sendo usado nos modelos M2/M3.
    assert planos[1]["consulta"] == "morph1ne videos bunkr"


def test_modelo_m2_usa_nome_base_e_host_aprovado():
    planos = cv.gerar_consultas_priorizadas(ARROBAS, ["vazado"], [], 30)
    m2 = [plano for plano in planos if plano["modelo"] == "M2"]
    assert m2
    assert m2[0]["consulta"] == "morph1ne videos bunkr"
    hosts = {plano["parametros"]["host"] for plano in m2}
    assert "bunkr.cr" in hosts
    assert "bunkrsearch.com" in hosts


def test_modelo_m3_variacoes_de_conteudo():
    planos = cv.gerar_consultas_priorizadas(ARROBAS, ["vazado"], [], 30)
    consultas = [p["consulta"] for p in planos if p["modelo"] == "M3"]
    assert consultas == [
        "morph1ne fotos",
        "morph1ne videos",
        "morph1ne album",
        "morph1ne solo",
    ]


def test_modelo_m4_fica_desligado_por_padrao():
    desligado = cv.gerar_consultas_priorizadas(ARROBAS, ["vazado"], [], 30)
    assert not any(p["modelo"] == "M4" for p in desligado)
    ligado = cv.gerar_consultas_priorizadas(
        ARROBAS, ["vazado"], [], 40, site_ativo=True
    )
    assert any(
        p["modelo"] == "M4" and p["consulta"] == "morph1ne site:bunkr.cr"
        for p in ligado
    )


def test_ordem_de_prioridade_dos_modelos():
    planos = cv.gerar_consultas_priorizadas(ARROBAS, ["vazado"], [], 30)
    ordem = ["M1", "M2", "M3", "M4", "M5"]
    posicoes = [ordem.index(p["modelo"]) for p in planos]
    assert posicoes == sorted(posicoes)


def test_limite_corta_pelos_modelos_de_menor_prioridade():
    planos = cv.gerar_consultas_priorizadas(ARROBAS, ["vazado"], [], 4)
    assert len(planos) == 4
    assert all(p["modelo"] in {"M1", "M2"} for p in planos)


def test_gera_consultas_por_arroba_com_limite():
    consultas = cv.gerar_consultas_busca(
        ["ana", "aninha"], ["vazado", "leaked"], ["erome.com"], 4
    )
    assert len(consultas) == 4
    assert consultas[0] == "ana OR aninha vazado"

    arroba_site = cv.gerar_consultas_busca(["ana"], ["vazado"], ["erome.com"], 30)
    assert '"@ana" site:erome.com' in arroba_site


def test_consultas_genericas_incluem_arroba_sozinho_e_nome():
    consultas = cv.gerar_consultas_busca(
        ["ana"], ["vazado"], [], 18, nome="Ana Silva"
    )
    assert '"@ana" "vazado"' in consultas
    assert '"@ana"' in consultas
    assert any("privacy" in consulta for consulta in consultas)
    assert '"Ana Silva"' in consultas
    assert '"Ana Silva" vazado' in consultas
    # Sem sites configurados os dominios conhecidos entram sozinhos.
    assert any("site:erome.com" in consulta for consulta in consultas)
    assert len(consultas) <= 18


def test_agrupa_em_poucas_consultas():
    sites = [f"s{i}.com" for i in range(30)]
    consultas = cv.montar_consultas("Ana", "@ana", ["vazado", "nova foto"], sites)
    assert len(consultas) == 3
    assert '"nova foto"' in consultas[0] and "tbs" not in consultas[0]
    assert consultas[1].count("site:") == 5
