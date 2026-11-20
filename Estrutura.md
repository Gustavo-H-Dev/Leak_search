app/
  main.py            rotas: o que cada endereço faz
  db.py              único lugar que conversa com o SQLite
  core/
    imagens.py       hash e comparação, isolados (pronto para virar worker depois)
  templates/
    base.html        layout comum
    home.html        (alterado)
    catalogo.html    cadastro de criadoras e fotos
    perfil_busca.html perfil público e fotos privadas de busca
    busca_web.html   busca na web unificada (SerpApi, Google Vision, Instagram)
    comparar.html    envio da imagem suspeita
compose.yaml         (alterado)
requirements.txt     (alterado)