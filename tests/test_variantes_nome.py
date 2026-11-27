from app.core import variantes_nome as vn


def test_variante_de_morph1ne2_0_inclui_nome_base():
    variantes = vn.gerar_variantes_nome("Morph1ne2.0")
    assert "morph1ne" in variantes
    assert "morph1ne2.0" in variantes
    assert "morph1ne20" in variantes


def test_variante_de_morph1neee_inclui_nome_base():
    variantes = vn.gerar_variantes_nome("Morph1neee_")
    assert "morph1ne" in variantes


def test_variantes_saem_em_minusculas_sem_arroba_e_sem_duplicadas():
    variantes = vn.gerar_variantes_nome("@Morph1neee_")
    assert all(v == v.lower() for v in variantes)
    assert all("@" not in v for v in variantes)
    assert len(variantes) == len(set(variantes))


def test_variantes_curtas_so_entram_com_a_flag():
    assert vn.gerar_variantes_nome("ana") == []
    assert "ana" in vn.gerar_variantes_nome("ana", incluir_curtas=True)


def test_nome_base_e_a_variante_comum_entre_os_arrobas():
    assert vn.nome_base(["Morph1ne2.0", "Morph1neee_"]) == "morph1ne"
    assert vn.nome_base(["ana"]) == ""


def test_digitos_do_meio_do_nome_sao_preservados():
    assert "morph1ne" in vn.gerar_variantes_nome("morph1ne")
    assert vn.gerar_variantes_nome("morph1ne") == ["morph1ne"]
