import re
import os
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db
from app.core import autenticacao
from app.main import app


class AutenticacaoTest(unittest.TestCase):
    def test_cadastro_ativa_conta_sem_smtp_quando_confirmacao_desativada(self):
        with TemporaryDirectory() as diretorio:
            with (
                patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"),
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_VERIFICATION_REQUIRED": "false",
                        "SMTP_HOST": "",
                        "SMTP_USER": "",
                        "SMTP_PASSWORD": "",
                    },
                ),
            ):
                db.iniciar_banco()
                with TestClient(app) as client:
                    formulario = client.get("/cadastro")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"', formulario.content
                    ).group(1).decode()
                    resposta_cadastro = client.post(
                        "/cadastro",
                        data={
                            "csrf_token": token_csrf,
                            "username": "nova_criadora",
                            "email": "nova@example.com",
                            "nome_publico": "Novo Perfil",
                            "arroba": "novo_perfil",
                            "senha": "senha-cliente-teste-forte",
                            "confirmar_senha": "senha-cliente-teste-forte",
                        },
                    )

                    login_formulario = client.get("/login")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"',
                        login_formulario.content,
                    ).group(1).decode()
                    resposta_login = client.post(
                        "/login",
                        data={
                            "csrf_token": token_csrf,
                            "username": "nova_criadora",
                            "senha": "senha-cliente-teste-forte",
                            "next": "/",
                        },
                        follow_redirects=False,
                    )

                usuario = db.obter_usuario_por_login("nova_criadora")
                self.assertEqual(resposta_cadastro.status_code, 200)
                self.assertIn("Sua conta já está ativa", resposta_cadastro.text)
                self.assertTrue(usuario["email_verified"])
                self.assertEqual(resposta_login.status_code, 303)
                self.assertEqual(resposta_login.headers["location"], "/")

    def test_cadastro_exige_smtp_quando_confirmacao_reativada(self):
        with TemporaryDirectory() as diretorio:
            with (
                patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"),
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_VERIFICATION_REQUIRED": "true",
                        "SMTP_HOST": "",
                        "SMTP_USER": "",
                        "SMTP_PASSWORD": "",
                    },
                ),
            ):
                db.iniciar_banco()
                with TestClient(app) as client:
                    formulario = client.get("/cadastro")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"', formulario.content
                    ).group(1).decode()
                    resposta = client.post(
                        "/cadastro",
                        data={
                            "csrf_token": token_csrf,
                            "username": "nova_criadora",
                            "email": "nova@example.com",
                            "nome_publico": "Novo Perfil",
                            "arroba": "novo_perfil",
                            "senha": "senha-cliente-teste-forte",
                            "confirmar_senha": "senha-cliente-teste-forte",
                        },
                    )

                self.assertEqual(resposta.status_code, 400)
                self.assertIn("envio de e-mail ainda não está configurado", resposta.text)
                self.assertIsNone(db.obter_usuario_por_login("nova_criadora"))

    def test_usuario_novo_e_cliente_com_data_de_cadastro_no_banco(self):
        with TemporaryDirectory() as diretorio:
            with patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"):
                db.iniciar_banco()
                user_id = db.criar_usuario(
                    "cliente_teste",
                    "cliente@example.com",
                    "hash-nao-utilizado-no-teste",
                )
                usuario = next(
                    user for user in db.listar_usuarios() if user["id"] == user_id
                )

                self.assertEqual(usuario["role"], "cliente")
                self.assertTrue(usuario["created_at"])

                with db.conexao() as conn:
                    conn.execute(
                        "UPDATE users SET role = 'user' WHERE id = ?", (user_id,)
                    )
                db._migrar_usuarios()
                self.assertEqual(
                    db.obter_usuario_por_id(user_id)["role"], "cliente"
                )

    def test_email_confirmado_nao_reivindica_perfil_antigo(self):
        with TemporaryDirectory() as diretorio:
            with patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"):
                db.iniciar_banco()
                criadora_id = db.criar_criadora_com_contato(
                    "Nome Público",
                    "criadora@example.com",
                    "perfil",
                )
                user_id = db.criar_usuario(
                    "criadora",
                    "CRIADORA@example.com",
                    "hash",
                    "Nome Público",
                    "perfil",
                )

                db.confirmar_email(user_id)

                criadora = next(
                    item
                    for item in db.listar_criadoras()
                    if item["id"] == criadora_id
                )
                self.assertIsNone(criadora["user_id"])
                self.assertTrue(db.obter_usuario_por_login("criadora")["email_verified"])

    def test_email_confirmado_nao_cria_perfil_separado(self):
        with TemporaryDirectory() as diretorio:
            with patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"):
                db.iniciar_banco()
                user_id = db.criar_usuario(
                    "nova_criadora",
                    "nova@example.com",
                    "hash",
                    "Novo Perfil",
                    "novo_perfil",
                )

                db.confirmar_email(user_id)

                self.assertFalse(db.listar_criadoras())
                self.assertTrue(db.obter_usuario_por_login("nova_criadora")["email_verified"])

    def test_pagina_e_alteracao_de_perfis_sao_restritas_a_admin(self):
        with TemporaryDirectory() as diretorio:
            with patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"):
                db.iniciar_banco()
                db.criar_admin_se_ausente(
                    "admin_teste",
                    "admin@example.com",
                    autenticacao.hash_senha("senha-admin-teste-forte"),
                )
                admin_id = db.obter_usuario_por_login("admin_teste")["id"]
                cliente_id = db.criar_usuario(
                    "cliente_teste",
                    "cliente@example.com",
                    autenticacao.hash_senha("senha-cliente-teste-forte"),
                    "Perfil Vinculado",
                    "perfil_vinculado",
                )
                db.confirmar_email(cliente_id)
                pendente_id = db.criar_usuario(
                    "pendente",
                    "pendente@example.com",
                    autenticacao.hash_senha("senha-cliente-teste-forte"),
                    "Perfil Pendente",
                    "perfil_pendente",
                )
                db.criar_criadora_com_contato("Perfil sem conta", "semconta@example.com")

                with TestClient(app) as cliente:
                    login = cliente.get("/login")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"', login.content
                    ).group(1).decode()
                    resposta_login = cliente.post(
                        "/login",
                        data={
                            "username": "cliente_teste",
                            "senha": "senha-cliente-teste-forte",
                            "next": "/",
                            "csrf_token": token_csrf,
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(resposta_login.status_code, 303)
                    pagina_cliente = cliente.get("/")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"', pagina_cliente.content
                    ).group(1).decode()
                    pagina_restrita = cliente.get("/usuarios")
                    catalogo_cliente = cliente.get("/catalogo")
                    busca_cliente = cliente.get("/buscar-serpapi")
                    arquivo_catalogo_cliente = cliente.get("/arquivos/nao-existe.png")
                    tentativa_alteracao = cliente.post(
                        f"/usuarios/{cliente_id}/perfil",
                        data={"role": "admin", "csrf_token": token_csrf},
                    )
                    self.assertEqual(cliente.get(f"/usuarios/{admin_id}/editar").status_code, 403)
                    self.assertEqual(cliente.post(
                        f"/usuarios/{admin_id}/excluir", data={"csrf_token": token_csrf}
                    ).status_code, 403)
                    self.assertEqual(cliente.post(
                        f"/usuarios/{admin_id}/editar", data={"csrf_token": token_csrf}
                    ).status_code, 403)

                self.assertEqual(pagina_restrita.status_code, 403)
                self.assertEqual(catalogo_cliente.status_code, 403)
                self.assertEqual(busca_cliente.status_code, 200)
                self.assertEqual(arquivo_catalogo_cliente.status_code, 403)
                self.assertNotIn(b'href="/usuarios"', pagina_cliente.content)
                self.assertEqual(tentativa_alteracao.status_code, 403)
                self.assertEqual(db.obter_usuario_por_id(cliente_id)["role"], "cliente")

                with TestClient(app) as admin:
                    login = admin.get("/login")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"', login.content
                    ).group(1).decode()
                    resposta_login = admin.post(
                        "/login",
                        data={
                            "username": "admin_teste",
                            "senha": "senha-admin-teste-forte",
                            "next": "/",
                            "csrf_token": token_csrf,
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(resposta_login.status_code, 303)
                    pagina_usuarios = admin.get("/usuarios")
                    pagina_catalogo = admin.get("/catalogo")
                    token_csrf = re.search(
                        rb'name="csrf_token" value="([^"]+)"',
                        pagina_usuarios.content,
                    ).group(1).decode()
                    auto_alteracao = admin.post(
                        f"/usuarios/{admin_id}/perfil",
                        data={"role": "cliente", "csrf_token": token_csrf},
                        follow_redirects=False,
                    )
                    atualizado = admin.post(
                        f"/usuarios/{cliente_id}/perfil",
                        data={"role": "admin", "csrf_token": token_csrf},
                        follow_redirects=False,
                    )
                    sem_csrf = admin.post(
                        f"/usuarios/{pendente_id}/perfil",
                        data={"role": "admin"},
                        follow_redirects=False,
                    )
                    pendente_atualizado = admin.post(
                        f"/usuarios/{pendente_id}/perfil",
                        data={"role": "admin", "csrf_token": token_csrf},
                        follow_redirects=False,
                    )

                self.assertEqual(pagina_usuarios.status_code, 200)
                self.assertIn(b'href="/usuarios"', pagina_usuarios.content)
                self.assertNotIn(b"cliente@example.com", pagina_usuarios.content)
                self.assertEqual(pagina_catalogo.status_code, 200)
                for pagina in (pagina_usuarios, pagina_catalogo):
                    self.assertIn("Perfil Vinculado", pagina.text)
                    self.assertNotIn("Perfil sem conta", pagina.text)
                    self.assertNotIn("pendente@example.com", pagina.text)
                    self.assertIn("Usuários cadastrados", pagina.text)
                    self.assertIn(f'href="/usuarios/{cliente_id}/editar"', pagina.text)
                    self.assertIn(f'href="/usuarios/{pendente_id}/editar"', pagina.text)
                    self.assertNotIn("<table", pagina.text)
                self.assertEqual(atualizado.status_code, 303)
                self.assertEqual(auto_alteracao.status_code, 303)
                self.assertEqual(sem_csrf.status_code, 403)
                self.assertEqual(pendente_atualizado.status_code, 303)
                self.assertEqual(db.obter_usuario_por_id(cliente_id)["role"], "admin")
                self.assertEqual(db.obter_usuario_por_id(pendente_id)["role"], "admin")
                self.assertEqual(
                    db.obter_usuario_por_login("admin_teste")["role"], "admin"
                )

    def test_admin_cria_conta_real_e_altera_permissao_por_checkbox(self):
        with (
            TemporaryDirectory() as diretorio,
            patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"),
            patch.dict(os.environ, {"EMAIL_VERIFICATION_REQUIRED": "false"}),
        ):
            db.iniciar_banco()
            db.criar_admin_se_ausente(
                "admin_teste", "admin@example.com",
                autenticacao.hash_senha("senha-admin-teste-forte"),
            )
            with TestClient(app) as admin:
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/login").text).group(1)
                admin.post("/login", data={
                    "username": "admin_teste", "senha": "senha-admin-teste-forte",
                    "csrf_token": token,
                })
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/usuarios/novo").text).group(1)
                dados = {
                    "csrf_token": token, "username": "ana", "email": "ana@example.com",
                    "nome_publico": "Ana", "arroba": "ana",
                    "senha": "senha-inicial-segura", "confirmar_senha": "senha-inicial-segura",
                    "role": "admin",
                }
                resposta = admin.post("/usuarios", data=dados, follow_redirects=False)
                usuario = db.obter_usuario_por_login("ana")
                self.assertEqual(resposta.status_code, 303)
                self.assertEqual(usuario["role"], "cliente")
                self.assertTrue(usuario["email_verified"])
                self.assertTrue(autenticacao.verificar_senha(usuario["password_hash"], dados["senha"]))
                self.assertNotEqual(usuario["password_hash"], dados["senha"])
                self.assertEqual(admin.post("/usuarios", data=dados).status_code, 400)
                usuario_id = usuario["id"]
                admin.post(f"/usuarios/{usuario_id}/perfil",
                           data={"csrf_token": token, "role": ["cliente", "admin"]})
                self.assertEqual(db.obter_usuario_por_id(usuario_id)["role"], "admin")
                admin.post(f"/usuarios/{usuario_id}/perfil",
                           data={"csrf_token": token, "role": "cliente"})
                self.assertEqual(db.obter_usuario_por_id(usuario_id)["role"], "cliente")

            with TestClient(app) as usuario_client:
                token = re.search(r'name="csrf_token" value="([^"]+)"', usuario_client.get("/login").text).group(1)
                login = usuario_client.post("/login", data={
                    "csrf_token": token, "username": "ana", "senha": dados["senha"],
                }, follow_redirects=False)
                self.assertEqual(login.status_code, 303)
                self.assertEqual(usuario_client.get("/usuarios").status_code, 403)
                self.assertEqual(usuario_client.post("/usuarios", data=dados).status_code, 403)

    def test_busca_usuario_e_restrita_ao_proprio_alvo(self):
        with (
            TemporaryDirectory() as diretorio,
            patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"),
            patch.dict(os.environ, {"EMAIL_VERIFICATION_REQUIRED": "false", "SERPAPI_API_KEY": ""}),
        ):
            db.iniciar_banco()
            senha_hash = autenticacao.hash_senha("senha-cliente-teste-forte")
            primeiro = db.criar_usuario("ana", "ana@example.com", senha_hash, "Ana", "ana")
            segundo = db.criar_usuario("bia", "bia@example.com", senha_hash, "Bia", "bia")
            db.criar_admin_se_ausente(
                "admin_teste", "admin@example.com",
                autenticacao.hash_senha("senha-admin-teste-forte"),
            )
            db.confirmar_email(primeiro)
            db.confirmar_email(segundo)
            with TestClient(app) as cliente:
                token = re.search(r'name="csrf_token" value="([^"]+)"', cliente.get("/login").text).group(1)
                cliente.post("/login", data={"username": "ana", "senha": "senha-cliente-teste-forte", "csrf_token": token})
                pagina = cliente.get("/busca-web")
                token = re.search(r'name="csrf_token" value="([^"]+)"', pagina.text).group(1)
                self.assertEqual(pagina.status_code, 200)
                self.assertNotIn('id="search-target"', pagina.text)
                self.assertNotIn("Uso da conta SerpApi", pagina.text)
                self.assertEqual(cliente.get(f"/busca-web?target_user_id={segundo}").status_code, 403)
                self.assertEqual(cliente.get(f"/valida?target_user_id={segundo}").status_code, 403)
                for endpoint in ("/buscar-vazamentos", "/buscar-serpapi"):
                    resposta = cliente.post(endpoint, data={
                        "csrf_token": token, "target_user_id": segundo,
                        "api": "google_images_light", "consentimento": "on",
                    })
                    self.assertEqual(resposta.status_code, 403)
                self.assertEqual(cliente.post("/busca-vazamentos/config", data={
                    "csrf_token": token, "termos": "alterado", "sites": "exemplo.com",
                }).status_code, 403)
                with patch("app.main._executar_busca_vazamentos") as executar:
                    resposta = cliente.post("/buscar-vazamentos", data={
                        "csrf_token": token, "api": "google_images_light", "consentimento": "on",
                    })
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(executar.call_args.args[0]["id"], primeiro)
                self.assertIn('"Ana"', executar.call_args.args[2][0])
            with TestClient(app) as admin:
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/login").text).group(1)
                admin.post("/login", data={
                    "username": "admin_teste", "senha": "senha-admin-teste-forte",
                    "csrf_token": token,
                })
                pagina = admin.get(f"/busca-web?target_user_id={segundo}")
                self.assertIn("Bia (@bia)", pagina.text)
                token = re.search(r'name="csrf_token" value="([^"]+)"', pagina.text).group(1)
                with patch("app.main._executar_busca_vazamentos") as executar:
                    resposta = admin.post("/buscar-vazamentos", data={
                        "csrf_token": token, "target_user_id": segundo,
                        "api": "google_images_light", "consentimento": "on",
                    })
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(executar.call_args.args[0]["id"], segundo)
                self.assertIn('"Bia"', executar.call_args.args[2][0])

    def test_usuario_criado_pelo_admin_respeita_confirmacao_de_email(self):
        with (
            TemporaryDirectory() as diretorio,
            patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"),
            patch.dict(os.environ, {"EMAIL_VERIFICATION_REQUIRED": "true"}),
            patch("app.main.autenticacao.email_configurado", return_value=True),
            patch("app.main._enviar_email_token") as enviar,
        ):
            db.iniciar_banco()
            db.criar_admin_se_ausente(
                "admin_teste", "admin@example.com",
                autenticacao.hash_senha("senha-admin-teste-forte"),
            )
            with TestClient(app) as admin:
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/login").text).group(1)
                admin.post("/login", data={
                    "username": "admin_teste", "senha": "senha-admin-teste-forte", "csrf_token": token,
                })
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/usuarios/novo").text).group(1)
                resposta = admin.post("/usuarios", data={
                    "csrf_token": token, "username": "ana", "email": "ana@example.com",
                    "nome_publico": "Ana", "arroba": "ana",
                    "senha": "senha-inicial-segura", "confirmar_senha": "senha-inicial-segura",
                }, follow_redirects=False)
                self.assertEqual(resposta.status_code, 303)
            usuario = db.obter_usuario_por_login("ana")
            self.assertFalse(usuario["email_verified"])
            enviar.assert_awaited_once_with(usuario["id"], "ana@example.com", "ana", "confirmar_email")
            with TestClient(app) as cliente:
                token = re.search(r'name="csrf_token" value="([^"]+)"', cliente.get("/login").text).group(1)
                resposta = cliente.post("/login", data={
                    "csrf_token": token, "username": "ana", "senha": "senha-inicial-segura",
                })
                self.assertEqual(resposta.status_code, 403)

    def test_editar_excluir_usuario_e_manter_dados_de_outras_contas(self):
        with (
            TemporaryDirectory() as diretorio,
            patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"),
            patch("app.main.PASTA_FOTOS_BUSCA", Path(diretorio)),
            patch("app.main.PASTA_OFICIAIS", Path(diretorio)),
            patch.dict(os.environ, {"EMAIL_VERIFICATION_REQUIRED": "false"}),
        ):
            db.iniciar_banco()
            senha_admin = "senha-admin-teste-forte"
            db.criar_admin_se_ausente("admin_teste", "admin@example.com", autenticacao.hash_senha(senha_admin))
            admin_id = db.obter_usuario_por_login("admin_teste")["id"]
            alvo = db.criar_usuario("ana", "ana@example.com", autenticacao.hash_senha("senha-inicial-segura"), "Ana", "ana")
            outra = db.criar_usuario("bia", "bia@example.com", "hash", "Bia", "bia")
            db.confirmar_email(alvo)
            db.adicionar_foto_busca(alvo, "foto.png", "foto.png")
            (Path(diretorio) / "foto.png").write_bytes(b"foto")
            criadora = db.criar_criadora_com_contato("Ana", "ana@example.com", "ana")
            with db.conexao() as conn:
                conn.execute("UPDATE criadoras SET user_id = ? WHERE id = ?", (alvo, criadora))
            db.adicionar_conteudo(criadora, "foto", "catalogo.png", "catalogo.png", ["a" * 16])
            (Path(diretorio) / "catalogo.png").write_bytes(b"foto")
            db.adicionar_site_oficial_usuario(outra, alvo, "Site", "https://example.com/bia")
            db.adicionar_site_vazado(outra, alvo, "https://example.com/relato", "Relato", "", "", "", "serp", {})
            db.adicionar_candidato(alvo, "https://example.com/alvo", "Alvo", "", "", "", "serp", "")
            db.salvar_snapshot_busca(admin_id, "busca", {"target_user_id": alvo})
            db.salvar_configuracao(f"vazamento_status_{alvo}", "{}")
            with TestClient(app) as admin:
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/login").text).group(1)
                admin.post("/login", data={"username": "admin_teste", "senha": senha_admin, "csrf_token": token})
                grid = admin.get("/usuarios")
                token = re.search(r'name="csrf_token" value="([^"]+)"', grid.text).group(1)
                self.assertIn("@ana", grid.text)
                self.assertNotIn("ana@example.com", grid.text)
                self.assertNotIn('name="role"', grid.text)
                editar = admin.get(f"/usuarios/{alvo}/editar")
                self.assertIn("ana@example.com", editar.text)
                self.assertIn('name="role"', editar.text)
                dados = {"csrf_token": token, "username": "ana_nova", "email": "nova@example.com",
                         "nome_publico": "Ana Nova", "arroba": "@ana_nova",
                         "role": ["cliente", "admin"], "senha": "senha-nova-segura",
                         "confirmar_senha": "senha-nova-segura"}
                conflito = {**dados, "email": "bia@example.com"}
                self.assertEqual(admin.post(f"/usuarios/{alvo}/editar", data=conflito).status_code, 400)
                self.assertEqual(db.obter_usuario_por_id(alvo)["email"], "ana@example.com")
                self.assertEqual(admin.post(f"/usuarios/{alvo}/editar", data={**dados, "csrf_token": ""}).status_code, 403)
                self.assertEqual(admin.post(f"/usuarios/{alvo}/editar", data=dados).status_code, 200)
                registro = db.obter_usuario_por_login("ana_nova")
                self.assertEqual((registro["role"], registro["email"]), ("admin", "nova@example.com"))
                self.assertFalse(registro["email_verified"])
                self.assertTrue(autenticacao.verificar_senha(registro["password_hash"], "senha-nova-segura"))
                self.assertEqual(db.obter_criadora(criadora)["handle"], "ana_nova")
                self.assertEqual(admin.post(f"/usuarios/{alvo}/excluir").status_code, 403)
                self.assertEqual(admin.post(f"/usuarios/{alvo}/excluir", data={"csrf_token": token}).status_code, 200)
            self.assertIsNone(db.obter_usuario_por_id(alvo))
            self.assertIsNone(db.obter_criadora(criadora))
            self.assertFalse((Path(diretorio) / "foto.png").exists())
            self.assertFalse((Path(diretorio) / "catalogo.png").exists())
            self.assertEqual(db.listar_candidatos(alvo), [])
            self.assertIsNone(db.obter_snapshot_busca(admin_id, "busca"))
            self.assertEqual(db.listar_sites_oficiais_usuario(outra)[0]["created_by"], outra)
            self.assertEqual(db.listar_sites_vazados(outra)[0]["reported_by"], outra)
            self.assertFalse(db.salvar_status_busca_usuario(
                alvo, f"vazamento_status_{alvo}", "{}"
            ))
            self.assertEqual(db.obter_configuracao(f"vazamento_status_{alvo}", ""), "")

    def test_autodemocao_e_autoexclusao_exigem_outro_admin(self):
        with (
            TemporaryDirectory() as diretorio,
            patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"),
            patch.dict(os.environ, {"EMAIL_VERIFICATION_REQUIRED": "false"}),
        ):
            db.iniciar_banco()
            senha = "senha-admin-teste-forte"
            db.criar_admin_se_ausente("admin_teste", "admin@example.com", autenticacao.hash_senha(senha))
            admin_id = db.obter_usuario_por_login("admin_teste")["id"]
            with TestClient(app) as admin:
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/login").text).group(1)
                admin.post("/login", data={"username": "admin_teste", "senha": senha, "csrf_token": token})
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/usuarios").text).group(1)
                resposta = admin.post(f"/usuarios/{admin_id}/excluir", data={"csrf_token": token})
                self.assertIn("pelo menos um administrador", resposta.text)
                self.assertIsNotNone(db.obter_usuario_por_id(admin_id))
                outro = db.criar_usuario("outro", "outro@example.com", "hash", "Outro", "outro")
                db.atualizar_perfil_usuario(outro, "admin")
                resposta = admin.post(f"/usuarios/{admin_id}/excluir", data={"csrf_token": token}, follow_redirects=False)
                self.assertTrue(resposta.headers["location"].startswith("/login?mensagem="))
                self.assertIsNone(db.obter_usuario_por_id(admin_id))
                self.assertFalse(db.criar_admin_se_ausente("admin_teste", "admin@example.com", "hash"))
                self.assertIsNone(db.obter_usuario_por_id(admin_id))

    def test_troca_email_envia_confirmacao_e_revoga_sessoes_e_tokens(self):
        with (
            TemporaryDirectory() as diretorio,
            patch.object(db, "CAMINHO_DB", Path(diretorio) / "auth.db"),
            patch.dict(os.environ, {"EMAIL_VERIFICATION_REQUIRED": "true"}),
            patch("app.main.autenticacao.email_configurado", return_value=True),
            patch("app.main._enviar_email_token") as enviar,
        ):
            db.iniciar_banco()
            db.criar_admin_se_ausente(
                "admin_teste", "admin@example.com",
                autenticacao.hash_senha("senha-admin-teste-forte"),
            )
            usuario_id = db.criar_usuario("ana", "ana@example.com", "hash", "Ana", "ana")
            db.confirmar_email(usuario_id)
            db.criar_sessao("sessao-antiga", usuario_id, "2099-01-01T00:00:00+00:00")
            db.salvar_token_auth("token-antigo", usuario_id, "confirmar_email", "2099-01-01T00:00:00+00:00")
            with TestClient(app) as admin:
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/login").text).group(1)
                admin.post("/login", data={"username": "admin_teste", "senha": "senha-admin-teste-forte", "csrf_token": token})
                token = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/usuarios").text).group(1)
                resposta = admin.post(f"/usuarios/{usuario_id}/editar", data={
                    "csrf_token": token, "username": "ana", "email": "nova@example.com",
                    "nome_publico": "Ana", "arroba": "ana", "role": "cliente",
                })
                self.assertIn("Confirme o novo e-mail", resposta.text)
            self.assertFalse(db.obter_usuario_por_login("ana")["email_verified"])
            self.assertIsNone(db.obter_usuario_da_sessao("sessao-antiga"))
            self.assertIsNone(db.consumir_token_auth("token-antigo", "confirmar_email"))
            enviar.assert_awaited_once_with(usuario_id, "nova@example.com", "ana", "confirmar_email")

    def test_remocao_de_perfis_sem_conta_preserva_conteudo_de_usuario(self):
        from app.main import _remover_criadoras_antigas

        with TemporaryDirectory() as diretorio:
            pasta = Path(diretorio)
            with (
                patch.object(db, "CAMINHO_DB", pasta / "auth.db"),
                patch("app.main.PASTA_OFICIAIS", pasta),
            ):
                db.iniciar_banco()
                usuario = db.criar_usuario("ana", "ana@example.com", "hash", "Ana", "ana")
                sem_conta = db.criar_criadora_com_contato("Antiga", "antiga@example.com")
                vinculada = db.criar_criadora_com_contato("Ana", "ana@example.com")
                with db.conexao() as conn:
                    conn.execute("UPDATE criadoras SET user_id = ? WHERE id = ?", (usuario, vinculada))
                for arquivo in ("antiga.png", "compartilhada.png"):
                    (pasta / arquivo).write_bytes(b"foto")
                    db.adicionar_conteudo(sem_conta, "foto", arquivo, arquivo, ["a" * 16])
                db.adicionar_conteudo(vinculada, "foto", "compartilhada.png", "foto", ["b" * 16])
                db.adicionar_site_oficial(sem_conta, "Site", "https://example.com/antiga")
                _remover_criadoras_antigas()
                _remover_criadoras_antigas()
                self.assertIsNone(db.obter_criadora(sem_conta))
                self.assertIsNotNone(db.obter_criadora(vinculada))
                self.assertFalse((pasta / "antiga.png").exists())
                self.assertTrue((pasta / "compartilhada.png").exists())
                self.assertEqual(len(db.listar_hashes_criadora_do_usuario(usuario)), 1)

    def test_usuario_com_catalogo_antigo_pode_usar_nome_publico_repetido(self):
        with TemporaryDirectory() as diretorio, patch.object(
            db, "CAMINHO_DB", Path(diretorio) / "auth.db"
        ):
            db.iniciar_banco()
            primeiro = db.criar_usuario("ana", "ana@example.com", "hash", "Ana", "ana")
            segundo = db.criar_usuario("bia", "bia@example.com", "hash", "Bia", "bia")
            primeira = db.criar_criadora_com_contato("Ana", "ana@example.com")
            segunda = db.criar_criadora_com_contato("Bia", "bia@example.com")
            with db.conexao() as conn:
                conn.execute("UPDATE criadoras SET user_id = ? WHERE id = ?", (primeiro, primeira))
                conn.execute("UPDATE criadoras SET user_id = ? WHERE id = ?", (segundo, segunda))
            db.atualizar_perfil_busca(segundo, "Ana", "nova_bia")
            self.assertEqual(db.obter_usuario_por_id(segundo)["public_name"], "Ana")
            self.assertEqual(db.obter_criadora(segunda)["handle"], "nova_bia")

    def test_hash_de_senha_verifica_sem_armazenar_senha_em_texto(self):
        senha = "uma-senha-forte-de-teste"
        password_hash = autenticacao.hash_senha(senha)

        self.assertNotEqual(password_hash, senha)
        self.assertTrue(autenticacao.verificar_senha(password_hash, senha))
        self.assertFalse(autenticacao.verificar_senha(password_hash, "senha errada"))

    def test_rejeita_senha_abaixo_do_minimo(self):
        with self.assertRaisesRegex(ValueError, "pelo menos 12"):
            autenticacao.hash_senha("curta")

    def test_token_auth_expira_e_so_pode_ser_usado_uma_vez(self):
        with TemporaryDirectory() as diretorio:
            caminho_db = Path(diretorio) / "auth.db"
            with patch.object(db, "CAMINHO_DB", caminho_db):
                db.iniciar_banco()
                user_id = db.criar_usuario(
                    "usuario_teste",
                    "teste@example.com",
                    "hash-nao-utilizado-no-teste",
                )
                db.salvar_token_auth(
                    "hash-token-valido",
                    user_id,
                    "confirmar_email",
                    (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                )
                db.salvar_token_auth(
                    "hash-token-expirado",
                    user_id,
                    "redefinir_senha",
                    (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                )

                self.assertEqual(
                    db.consumir_token_auth("hash-token-valido", "confirmar_email"),
                    user_id,
                )
                self.assertIsNone(
                    db.consumir_token_auth("hash-token-valido", "confirmar_email")
                )
                self.assertIsNone(
                    db.consumir_token_auth("hash-token-expirado", "redefinir_senha")
                )

    def test_home_e_login_sao_publicos_e_catalogo_exige_sessao(self):
        with TestClient(app) as client:
            home = client.get("/", follow_redirects=False)
            login = client.get("/login", follow_redirects=False)
            catalogo = client.get("/catalogo", follow_redirects=False)
            cadastro = client.get("/cadastro", follow_redirects=False)
            recuperar = client.get("/esqueci-senha", follow_redirects=False)
            csrf_ausente = client.post(
                "/login",
                data={"username": "usuario", "senha": "senha"},
                follow_redirects=False,
            )

        self.assertEqual(home.status_code, 200)
        self.assertEqual(login.status_code, 200)
        self.assertIn(b'name="csrf_token"', login.content)
        self.assertEqual(cadastro.status_code, 200)
        self.assertIn(b'name="csrf_token"', cadastro.content)
        self.assertEqual(recuperar.status_code, 200)
        self.assertEqual(csrf_ausente.status_code, 403)
        self.assertEqual(catalogo.status_code, 303)
        self.assertEqual(catalogo.headers["location"], "/login?next=/catalogo")


if __name__ == "__main__":
    unittest.main()
