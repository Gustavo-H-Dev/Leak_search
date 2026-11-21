import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

CAMINHO_DB = Path("data/catalogo.db")
INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)


class _PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, parametros=()):
        query = re.sub(r"\?", "%s", query)
        return self._connection.execute(query, parametros)


def _url_postgresql() -> str | None:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def usando_postgresql() -> bool:
    return _url_postgresql() is not None


@contextmanager
def conexao():
    url_postgresql = _url_postgresql()
    if url_postgresql:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(url_postgresql, row_factory=dict_row) as connection:
            yield _PostgresConnection(connection)
        return

    CAMINHO_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CAMINHO_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def iniciar_banco():
    if usando_postgresql():
        _iniciar_banco_postgresql()
        _migrar_sites_oficiais()
        _migrar_usuarios()
        _migrar_criadoras()
        return

    with conexao() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS criadoras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
                email TEXT,
                handle TEXT,
                user_id INTEGER UNIQUE,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS conteudos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criadora_id INTEGER NOT NULL REFERENCES criadoras(id),
                tipo TEXT NOT NULL,
                arquivo TEXT NOT NULL,
                nome_original TEXT,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS hashes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conteudo_id INTEGER NOT NULL REFERENCES conteudos(id),
                posicao INTEGER NOT NULL DEFAULT 0,
                phash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sites_oficiais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criadora_id INTEGER REFERENCES criadoras(id),
                nome TEXT NOT NULL,
                url TEXT NOT NULL,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'cliente',
                public_name TEXT,
                public_handle TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_search_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                arquivo TEXT NOT NULL UNIQUE,
                nome_original TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_official_sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_by INTEGER NOT NULL REFERENCES users(id),
                nome TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, url)
            );
            CREATE TABLE IF NOT EXISTS leak_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reported_by INTEGER NOT NULL REFERENCES users(id),
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                snippet TEXT NOT NULL DEFAULT '',
                thumbnail TEXT NOT NULL DEFAULT '',
                engine TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, url)
            );
            CREATE TABLE IF NOT EXISTS image_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                title TEXT NOT NULL,
                engine TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                phash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, sha256)
            );
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                purpose TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_snapshots (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, kind)
            );
            CREATE INDEX IF NOT EXISTS search_snapshots_updated_at
            ON search_snapshots (updated_at);
            """
        )
    _migrar_sites_oficiais()
    _migrar_usuarios()
    _migrar_criadoras()


def _iniciar_banco_postgresql():
    comandos = (
        """
        CREATE TABLE IF NOT EXISTS criadoras (
            id BIGSERIAL PRIMARY KEY,
            nome TEXT NOT NULL UNIQUE,
            email TEXT,
            handle TEXT,
            user_id BIGINT UNIQUE,
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS conteudos (
            id BIGSERIAL PRIMARY KEY,
            criadora_id BIGINT NOT NULL REFERENCES criadoras(id),
            tipo TEXT NOT NULL,
            arquivo TEXT NOT NULL,
            nome_original TEXT,
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hashes (
            id BIGSERIAL PRIMARY KEY,
            conteudo_id BIGINT NOT NULL REFERENCES conteudos(id),
            posicao INTEGER NOT NULL DEFAULT 0,
            phash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sites_oficiais (
            id BIGSERIAL PRIMARY KEY,
            criadora_id BIGINT REFERENCES criadoras(id),
            nome TEXT NOT NULL,
            url TEXT NOT NULL,
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            email_verified INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL DEFAULT 'cliente',
            public_name TEXT,
            public_handle TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_search_photos (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            arquivo TEXT NOT NULL UNIQUE,
            nome_original TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_official_sites (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_by BIGINT NOT NULL REFERENCES users(id),
            nome TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, url)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS leak_reports (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reported_by BIGINT NOT NULL REFERENCES users(id),
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            snippet TEXT NOT NULL DEFAULT '',
            thumbnail TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, url)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS image_candidates (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_url TEXT NOT NULL,
            title TEXT NOT NULL,
            engine TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            phash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, sha256)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS search_snapshots (
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, kind)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS search_snapshots_updated_at
        ON search_snapshots (updated_at)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower
        ON users (lower(username))
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower
        ON users (lower(email))
        """,
    )
    with conexao() as conn:
        for comando in comandos:
            conn.execute(comando)


def _migrar_sites_oficiais():
    with conexao() as conn:
        if usando_postgresql():
            colunas = {
                coluna["column_name"]
                for coluna in conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'sites_oficiais'
                    """
                ).fetchall()
            }
        else:
            colunas = {
                coluna["name"]
                for coluna in conn.execute("PRAGMA table_info(sites_oficiais)")
            }
        if "dominio" in colunas:
            conn.execute(
                "ALTER TABLE sites_oficiais RENAME TO sites_oficiais_antigos"
            )
            tipo_id = "BIGSERIAL" if usando_postgresql() else "INTEGER"
            tipo_ref = "BIGINT" if usando_postgresql() else "INTEGER"
            conn.execute(f"""
                CREATE TABLE sites_oficiais (
                    id {tipo_id} PRIMARY KEY,
                    criadora_id {tipo_ref} REFERENCES criadoras(id),
                    nome TEXT NOT NULL,
                    url TEXT NOT NULL,
                    criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """)
            antigos = conn.execute(
                "SELECT id, nome, dominio, criado_em FROM sites_oficiais_antigos"
            ).fetchall()
            for site in antigos:
                conn.execute(
                    """
                    INSERT INTO sites_oficiais (id, nome, url, criado_em)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        site["id"],
                        site["nome"],
                        f"https://{site['dominio']}/",
                        site["criado_em"],
                    ),
                )
            conn.execute("DROP TABLE sites_oficiais_antigos")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS sites_oficiais_criadora_url
            ON sites_oficiais (criadora_id, url)
            """
        )


def _migrar_usuarios():
    with conexao() as conn:
        if usando_postgresql():
            conn.execute(
                "ALTER TABLE users ALTER COLUMN role SET DEFAULT 'cliente'"
            )
            conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS public_name TEXT"
            )
            conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS public_handle TEXT"
            )
        else:
            colunas = {
                coluna["name"] for coluna in conn.execute("PRAGMA table_info(users)")
            }
            if "public_name" not in colunas:
                conn.execute("ALTER TABLE users ADD COLUMN public_name TEXT")
            if "public_handle" not in colunas:
                conn.execute("ALTER TABLE users ADD COLUMN public_handle TEXT")
        conn.execute(
            "UPDATE users SET role = 'cliente' WHERE role = 'user'"
        )


def _migrar_criadoras():
    with conexao() as conn:
        if usando_postgresql():
            conn.execute("ALTER TABLE criadoras ADD COLUMN IF NOT EXISTS email TEXT")
            conn.execute("ALTER TABLE criadoras ADD COLUMN IF NOT EXISTS handle TEXT")
            conn.execute(
                "ALTER TABLE criadoras ADD COLUMN IF NOT EXISTS user_id BIGINT"
            )
        else:
            colunas = {
                coluna["name"]
                for coluna in conn.execute("PRAGMA table_info(criadoras)")
            }
            if "email" not in colunas:
                conn.execute("ALTER TABLE criadoras ADD COLUMN email TEXT")
            if "handle" not in colunas:
                conn.execute("ALTER TABLE criadoras ADD COLUMN handle TEXT")
            if "user_id" not in colunas:
                conn.execute("ALTER TABLE criadoras ADD COLUMN user_id INTEGER")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS criadoras_user_id_unique
            ON criadoras (user_id)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS criadoras_email_unique
            ON criadoras (lower(email))
            WHERE email IS NOT NULL AND email != ''
            """
        )


def _agora_utc():
    return datetime.now(timezone.utc).isoformat()


def criar_admin_se_ausente(username, email, password_hash):
    with conexao() as conn:
        existente = conn.execute(
            "SELECT id, role FROM users WHERE lower(username) = lower(?)",
            (username,),
        ).fetchone()
        if existente:
            if existente["role"] != "admin":
                raise ValueError(
                    "O login de administrador já está ocupado por uma conta não-admin."
                )
            return False
        conn.execute(
            """
            INSERT INTO users (username, email, password_hash, email_verified, role)
            VALUES (?, ?, ?, 1, 'admin')
            """,
            (username, email, password_hash),
        )
        return True


def criar_usuario(
    username, email, password_hash, public_name=None, public_handle=None
):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (
                username, email, password_hash, role, public_name, public_handle
            )
            VALUES (?, ?, ?, 'cliente', ?, ?)
            RETURNING id
            """,
            (username, email, password_hash, public_name, public_handle),
        )
        return cur.fetchone()["id"]


def obter_usuario_por_login(login):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, username, email, password_hash, email_verified, role
            FROM users WHERE lower(username) = lower(?)
               OR lower(email) = lower(?)
            LIMIT 1
            """,
            (login, login),
        ).fetchone()


def obter_usuario_por_email(email):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, username, email, password_hash, email_verified, role
            FROM users WHERE lower(email) = lower(?)
            """,
            (email,),
        ).fetchone()


def obter_usuario_por_id(user_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, username, email, role, public_name, public_handle
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()


def listar_usuarios():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, username, email, email_verified, role, public_name,
                   public_handle, created_at,
                   (SELECT id FROM criadoras WHERE user_id = users.id) AS criadora_id
            FROM users
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()


def atualizar_perfil_usuario(user_id, role):
    if role not in {"admin", "cliente"}:
        raise ValueError("Perfil de usuário inválido.")
    with conexao() as conn:
        cur = conn.execute(
            "UPDATE users SET role = ? WHERE id = ?",
            (role, user_id),
        )
        return cur.rowcount > 0


def atualizar_perfil_busca(user_id, public_name, public_handle):
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE users
            SET public_name = ?, public_handle = ?
            WHERE id = ?
            """,
            (public_name, public_handle, user_id),
        )
        atualizado = cur.rowcount > 0
        conn.execute(
            """
            UPDATE criadoras SET nome = ?, handle = ?
            WHERE user_id = ?
            """,
            (public_name, public_handle, user_id),
        )
        return atualizado


def perfil_criadora_disponivel(nome, email):
    with conexao() as conn:
        mesma_email = conn.execute(
            """
            SELECT id FROM criadoras
            WHERE lower(email) = lower(?) AND user_id IS NULL
            """,
            (email,),
        ).fetchone()
        nome_existente = conn.execute(
            "SELECT id FROM criadoras WHERE lower(nome) = lower(?)",
            (nome,),
        ).fetchone()
        if nome_existente is None:
            return True
        return mesma_email is not None and mesma_email["id"] == nome_existente["id"]


def listar_usuarios_com_perfil_busca():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, username, public_name, public_handle
            FROM users
            WHERE public_name IS NOT NULL AND public_handle IS NOT NULL
            ORDER BY public_name, username
            """
        ).fetchall()


def adicionar_foto_busca(user_id, arquivo, nome_original):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO user_search_photos (user_id, arquivo, nome_original)
            VALUES (?, ?, ?)
            RETURNING id
            """,
            (user_id, arquivo, nome_original),
        )
        return cur.fetchone()["id"]


def listar_fotos_busca(user_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, user_id, arquivo, nome_original, created_at
            FROM user_search_photos
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()


def obter_foto_busca(foto_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, user_id, arquivo, nome_original
            FROM user_search_photos
            WHERE id = ?
            """,
            (foto_id,),
        ).fetchone()


def excluir_foto_busca(foto_id, user_id):
    with conexao() as conn:
        cur = conn.execute(
            "DELETE FROM user_search_photos WHERE id = ? AND user_id = ?",
            (foto_id, user_id),
        )
        return cur.rowcount > 0


def adicionar_site_oficial_usuario(user_id, created_by, nome, url):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO user_official_sites (user_id, created_by, nome, url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, url) DO NOTHING
            RETURNING id
            """,
            (user_id, created_by, nome, url),
        )
        registro = cur.fetchone()
        return registro["id"] if registro else None


def listar_sites_oficiais_usuario(user_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, user_id, created_by, nome, url, created_at
            FROM user_official_sites
            WHERE user_id = ?
            ORDER BY nome, url
            """,
            (user_id,),
        ).fetchall()


def listar_paginas_oficiais_com_donos():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT s.url, c.user_id AS user_id
            FROM sites_oficiais s
            LEFT JOIN criadoras c ON c.id = s.criadora_id
            UNION ALL
            SELECT url, user_id FROM user_official_sites
            """
        ).fetchall()


def adicionar_candidato_imagem(
    user_id, source_url, title, engine, sha256, phash
):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO image_candidates (
                user_id, source_url, title, engine, sha256, phash
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, sha256) DO NOTHING
            RETURNING id
            """,
            (user_id, source_url, title, engine, sha256, phash),
        )
        registro = cur.fetchone()
        return registro["id"] if registro else None


def listar_candidatos_imagem(user_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, user_id, source_url, title, engine, sha256, phash,
                   status, created_at
            FROM image_candidates
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()


def atualizar_status_candidato_imagem(candidate_id, user_id, status):
    if status not in {"reviewed", "dismissed"}:
        raise ValueError("Status de candidato inválido.")
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE image_candidates SET status = ?
            WHERE id = ? AND user_id = ?
            """,
            (status, candidate_id, user_id),
        )
        return cur.rowcount > 0


def excluir_site_oficial_usuario(site_id, user_id):
    with conexao() as conn:
        cur = conn.execute(
            "DELETE FROM user_official_sites WHERE id = ? AND user_id = ?",
            (site_id, user_id),
        )
        return cur.rowcount > 0


def adicionar_site_vazado(
    user_id,
    reported_by,
    url,
    title,
    source,
    snippet,
    thumbnail,
    engine,
    metadata,
):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO leak_reports (
                user_id, reported_by, url, title, source, snippet,
                thumbnail, engine, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, url) DO NOTHING
            RETURNING id
            """,
            (
                user_id,
                reported_by,
                url,
                title,
                source,
                snippet,
                thumbnail,
                engine,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        registro = cur.fetchone()
        return registro["id"] if registro else None


def listar_sites_vazados(user_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT r.id, r.user_id, r.reported_by, r.url, r.title,
                   r.source, r.snippet, r.thumbnail, r.engine,
                   r.metadata_json, r.created_at, u.username AS reporter
            FROM leak_reports r
            JOIN users u ON u.id = r.reported_by
            WHERE r.user_id = ?
            ORDER BY r.created_at DESC, r.id DESC
            """,
            (user_id,),
        ).fetchall()


# Uma linha por usuário e tipo (upsert), então a tabela não cresce com o uso;
# a limpeza por updated_at remove usuários inativos.
RETENCAO_SNAPSHOTS = timedelta(days=30)


def salvar_snapshot_busca(user_id, kind, payload):
    agora = _agora_utc()
    limite = (datetime.now(timezone.utc) - RETENCAO_SNAPSHOTS).isoformat()
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO search_snapshots (user_id, kind, payload_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, kind) DO UPDATE
            SET payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (user_id, kind, json.dumps(payload, ensure_ascii=False, default=str), agora),
        )
        conn.execute("DELETE FROM search_snapshots WHERE updated_at < ?", (limite,))


def obter_snapshot_busca(user_id, kind):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT payload_json, updated_at FROM search_snapshots
            WHERE user_id = ? AND kind = ?
            """,
            (user_id, kind),
        ).fetchone()
    if not linha:
        return None
    try:
        return {"dados": json.loads(linha["payload_json"]), "updated_at": linha["updated_at"]}
    except ValueError:
        return None


def atualizar_senha(user_id, password_hash):
    with conexao() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )


def confirmar_email(user_id):
    with conexao() as conn:
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))
        usuario = conn.execute(
            """
            SELECT email, public_name, public_handle
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if not usuario or not usuario["public_name"] or not usuario["public_handle"]:
            return

        criadora = conn.execute(
            """
            SELECT id FROM criadoras
            WHERE lower(email) = lower(?) AND user_id IS NULL
            """,
            (usuario["email"],),
        ).fetchone()
        if criadora:
            conn.execute(
                """
                UPDATE criadoras SET user_id = ?, nome = ?, handle = ?
                WHERE id = ? AND user_id IS NULL
                """,
                (
                    user_id,
                    usuario["public_name"],
                    usuario["public_handle"],
                    criadora["id"],
                ),
            )
            return

        conn.execute(
            """
            INSERT INTO criadoras (nome, email, handle, user_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                usuario["public_name"],
                usuario["email"],
                usuario["public_handle"],
                user_id,
            ),
        )


def salvar_token_auth(token_hash, user_id, purpose, expires_at):
    with conexao() as conn:
        conn.execute(
            "DELETE FROM auth_tokens WHERE user_id = ? AND purpose = ?",
            (user_id, purpose),
        )
        conn.execute(
            """
            INSERT INTO auth_tokens (token_hash, user_id, purpose, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_hash, user_id, purpose, expires_at),
        )


def consumir_token_auth(token_hash, purpose):
    agora = _agora_utc()
    with conexao() as conn:
        token = conn.execute(
            """
            SELECT user_id, expires_at FROM auth_tokens
            WHERE token_hash = ? AND purpose = ?
            """,
            (token_hash, purpose),
        ).fetchone()
        if token is None:
            return None
        conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))
        if token["expires_at"] <= agora:
            return None
        return token["user_id"]


def criar_sessao(token_hash, user_id, expires_at):
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES (?, ?, ?)
            """,
            (token_hash, user_id, expires_at),
        )


def obter_usuario_da_sessao(token_hash):
    agora = _agora_utc()
    with conexao() as conn:
        return conn.execute(
            """
            SELECT u.id, u.username, u.email, u.role,
                   u.public_name, u.public_handle
            FROM auth_sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, agora),
        ).fetchone()


def revogar_sessao(token_hash):
    with conexao() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


def revogar_sessoes_usuario(user_id):
    with conexao() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))


def obter_segredo_sessao():
    import secrets

    with conexao() as conn:
        configurado = conn.execute(
            "SELECT value FROM app_settings WHERE name = 'session_secret'"
        ).fetchone()
        if configurado:
            return configurado["value"]
        segredo = secrets.token_urlsafe(48)
        conn.execute(
            "INSERT INTO app_settings (name, value) VALUES ('session_secret', ?)",
            (segredo,),
        )
        return segredo


def criar_criadora(nome):
    with conexao() as conn:
        if usando_postgresql():
            conn.execute(
                "INSERT INTO criadoras (nome) VALUES (?) ON CONFLICT DO NOTHING",
                (nome,),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO criadoras (nome) VALUES (?)", (nome,)
            )
        return conn.execute(
            "SELECT id FROM criadoras WHERE nome = ?", (nome,)
        ).fetchone()["id"]


def listar_criadoras():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT c.id, c.nome, c.email, c.handle, c.user_id,
                   u.username, u.email_verified
            FROM criadoras c
            LEFT JOIN users u ON u.id = c.user_id
            ORDER BY c.nome
            """
        ).fetchall()


def criar_criadora_com_contato(nome, email, handle=None):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO criadoras (nome, email, handle)
            VALUES (?, ?, ?)
            RETURNING id
            """,
            (nome, email or None, handle or None),
        )
        return cur.fetchone()["id"]


def atualizar_criadora_contato(criadora_id, nome, email, handle=None):
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE criadoras SET nome = ?, email = ?, handle = ?
            WHERE id = ? AND user_id IS NULL
            """,
            (nome, email or None, handle or None, criadora_id),
        )
        return cur.rowcount > 0


def adicionar_site_oficial(criadora_id, nome, url):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO sites_oficiais (criadora_id, nome, url)
            VALUES (?, ?, ?)
            RETURNING id
            """,
            (criadora_id, nome, url),
        )
        return cur.fetchone()["id"]


def listar_sites_oficiais():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT s.id, s.criadora_id, c.nome AS criadora,
                   s.nome, s.url, s.criado_em
            FROM sites_oficiais s
            LEFT JOIN criadoras c ON c.id = s.criadora_id
            ORDER BY c.nome, s.url
            """
        ).fetchall()


def atualizar_site_oficial(site_id, criadora_id, nome, url):
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE sites_oficiais
            SET criadora_id = ?, nome = ?, url = ?
            WHERE id = ?
            """,
            (criadora_id, nome, url, site_id),
        )
        return cur.rowcount > 0


def excluir_site_oficial(site_id):
    with conexao() as conn:
        cur = conn.execute("DELETE FROM sites_oficiais WHERE id = ?", (site_id,))
        return cur.rowcount > 0


def adicionar_conteudo(criadora_id, tipo, arquivo, nome_original, phashes):
    with conexao() as conn:
        cur = conn.execute(
            "INSERT INTO conteudos (criadora_id, tipo, arquivo, nome_original) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (criadora_id, tipo, arquivo, nome_original),
        )
        conteudo_id = cur.fetchone()["id"]
        for posicao, phash in enumerate(phashes):
            conn.execute(
                "INSERT INTO hashes (conteudo_id, posicao, phash) VALUES (?, ?, ?)",
                (conteudo_id, posicao, phash),
            )
        return conteudo_id


def listar_conteudos():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT c.id, c.criadora_id, c.tipo, c.arquivo, c.nome_original,
                   c.criado_em, cr.nome AS criadora
            FROM conteudos c
            JOIN criadoras cr ON cr.id = c.criadora_id
            ORDER BY c.id DESC
            """
        ).fetchall()


def obter_conteudo(conteudo_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, criadora_id, arquivo, nome_original
            FROM conteudos WHERE id = ?
            """,
            (conteudo_id,),
        ).fetchone()


def excluir_conteudo(conteudo_id):
    with conexao() as conn:
        conteudo = conn.execute(
            "SELECT arquivo FROM conteudos WHERE id = ?", (conteudo_id,)
        ).fetchone()
        if conteudo is None:
            return None

        conn.execute("DELETE FROM hashes WHERE conteudo_id = ?", (conteudo_id,))
        conn.execute("DELETE FROM conteudos WHERE id = ?", (conteudo_id,))
        return conteudo["arquivo"]


def listar_hashes():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT h.phash, h.posicao, c.id AS conteudo_id, c.arquivo, c.tipo,
                   cr.nome AS criadora
            FROM hashes h
            JOIN conteudos c ON c.id = h.conteudo_id
            JOIN criadoras cr ON cr.id = c.criadora_id
            """
        ).fetchall()


def listar_hashes_criadora_do_usuario(user_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT h.phash, h.posicao, c.id AS conteudo_id, c.arquivo, c.tipo,
                   cr.nome AS criadora
            FROM hashes h
            JOIN conteudos c ON c.id = h.conteudo_id
            JOIN criadoras cr ON cr.id = c.criadora_id
            WHERE cr.user_id = ?
            """,
            (user_id,),
        ).fetchall()