import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CAMINHO_DB = Path("data/catalogo.db")


@contextmanager
def conexao():
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
    with conexao() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS criadoras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            """
        )


def _agora_utc():
    return datetime.now(timezone.utc).isoformat()


def criar_admin_se_ausente(username, email, password_hash):
    with conexao() as conn:
        existente = conn.execute(
            "SELECT id, role FROM users WHERE username = ? COLLATE NOCASE",
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


def criar_usuario(username, email, password_hash):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES (?, ?, ?)
            """,
            (username, email, password_hash),
        )
        return cur.lastrowid


def obter_usuario_por_login(login):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, username, email, password_hash, email_verified, role
            FROM users WHERE username = ? COLLATE NOCASE
               OR email = ? COLLATE NOCASE
            LIMIT 1
            """,
            (login, login),
        ).fetchone()


def obter_usuario_por_email(email):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, username, email, password_hash, email_verified, role
            FROM users WHERE email = ? COLLATE NOCASE
            """,
            (email,),
        ).fetchone()


def obter_usuario_por_id(user_id):
    with conexao() as conn:
        return conn.execute(
            "SELECT id, username, email, role FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def atualizar_senha(user_id, password_hash):
    with conexao() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )


def confirmar_email(user_id):
    with conexao() as conn:
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))


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
            SELECT u.id, u.username, u.email, u.role
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
        conn.execute("INSERT OR IGNORE INTO criadoras (nome) VALUES (?)", (nome,))


def listar_criadoras():
    with conexao() as conn:
        return conn.execute("SELECT id, nome FROM criadoras ORDER BY nome").fetchall()


def adicionar_conteudo(criadora_id, tipo, arquivo, nome_original, phashes):
    with conexao() as conn:
        cur = conn.execute(
            "INSERT INTO conteudos (criadora_id, tipo, arquivo, nome_original) "
            "VALUES (?, ?, ?, ?)",
            (criadora_id, tipo, arquivo, nome_original),
        )
        conteudo_id = cur.lastrowid
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
            SELECT c.id, c.tipo, c.arquivo, c.nome_original, c.criado_em,
                   cr.nome AS criadora
            FROM conteudos c
            JOIN criadoras cr ON cr.id = c.criadora_id
            ORDER BY c.id DESC
            """
        ).fetchall()


def obter_conteudo(conteudo_id):
    with conexao() as conn:
        return conn.execute(
            "SELECT id, arquivo, nome_original FROM conteudos WHERE id = ?",
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