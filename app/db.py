import sqlite3
from contextlib import contextmanager
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
            """
        )


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