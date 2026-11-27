import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

CAMINHO_DB = Path("data/catalogo.db")
INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
logger = logging.getLogger(__name__)


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


def _valor_json(valor):
    """JSONB no PostgreSQL, texto JSON no SQLite de teste."""
    if usando_postgresql():
        from psycopg.types.json import Jsonb

        return Jsonb(valor if valor is not None else {})
    return json.dumps(valor, ensure_ascii=False, default=str)


def _usar_sqlite_de_teste() -> bool:
    """SQLite existe apenas para a suite de testes, nunca para a aplicacao.

    Em desenvolvimento e producao o banco e sempre o PostgreSQL apontado por
    DATABASE_URL; o compose e o Render definem essa variavel.
    """
    return os.getenv("APP_TESTES_SQLITE", "").strip() == "1"


@contextmanager
def conexao():
    url_postgresql = _url_postgresql()
    if url_postgresql:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(url_postgresql, row_factory=dict_row) as connection:
            yield _PostgresConnection(connection)
        return

    if not _usar_sqlite_de_teste():
        raise RuntimeError(
            "Defina DATABASE_URL com a URL do PostgreSQL. "
            "A aplicacao nao usa mais SQLite em execucao normal."
        )

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
    if not usando_postgresql() and not _usar_sqlite_de_teste():
        raise RuntimeError(
            "Defina DATABASE_URL com a URL do PostgreSQL. "
            "A aplicacao nao usa mais SQLite em execucao normal."
        )
    if usando_postgresql():
        _iniciar_banco_postgresql()
        _migrar_sites_oficiais()
        _migrar_usuarios()
        _migrar_criadoras()
        _migrar_busca_provedores()
        try:
            with conexao() as conn:
                conn.execute(
                    "ALTER TABLE instagram_post_hashes ADD COLUMN IF NOT EXISTS miniatura TEXT NOT NULL DEFAULT ''"
                )
        except Exception:
            pass
        _semear_dominios_iniciais()
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
            CREATE TABLE IF NOT EXISTS perfil_monitorado (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criadora_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                plataforma TEXT NOT NULL,
                arroba TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ativo',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (criadora_id, plataforma, arroba)
            );
            CREATE TABLE IF NOT EXISTS nome_variante (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criadora_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                texto TEXT NOT NULL,
                ativo INTEGER NOT NULL DEFAULT 1,
                origem TEXT NOT NULL DEFAULT 'manual',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (criadora_id, texto)
            );
            CREATE TABLE IF NOT EXISTS pagina_oficial (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criadora_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                dominio TEXT NOT NULL DEFAULT '',
                origem TEXT NOT NULL DEFAULT 'manual',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (criadora_id, url)
            );
            CREATE INDEX IF NOT EXISTS perfil_monitorado_criadora
            ON perfil_monitorado (criadora_id, status);
            CREATE INDEX IF NOT EXISTS nome_variante_criadora
            ON nome_variante (criadora_id, ativo);
            -- Lista antiga de sites oficiais por conta: substituida por
            -- pagina_oficial, que agora e a unica fonte desse dado.
            DROP TABLE IF EXISTS user_official_sites;
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
            CREATE TABLE IF NOT EXISTS search_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                snippet TEXT NOT NULL DEFAULT '',
                thumbnail TEXT NOT NULL DEFAULT '',
                engine TEXT NOT NULL,
                phash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, url)
            );
            CREATE TABLE IF NOT EXISTS official_image_hashes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                phash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, url)
            );
            CREATE TABLE IF NOT EXISTS instagram_post_hashes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                shortcode TEXT NOT NULL,
                url TEXT NOT NULL,
                miniatura TEXT NOT NULL DEFAULT '',
                phash TEXT NOT NULL,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, shortcode)
            );
            CREATE TABLE IF NOT EXISTS instagram_busca_controle (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ultima_busca_em TEXT NOT NULL,
                UNIQUE (user_id)
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
            CREATE TABLE IF NOT EXISTS busca_execucao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criadora_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provedor TEXT NOT NULL,
                termos_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pendente',
                erro_instagram TEXT,
                erro TEXT,
                resumo_json TEXT NOT NULL DEFAULT '{}',
                criada_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                concluida_em TEXT
            );
            CREATE TABLE IF NOT EXISTS agendamento_busca (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criadora_id INTEGER NOT NULL UNIQUE
                    REFERENCES users(id) ON DELETE CASCADE,
                frequencia TEXT NOT NULL DEFAULT 'semanal',
                hora INTEGER NOT NULL DEFAULT 9,
                dia_semana INTEGER NOT NULL DEFAULT 1,
                ativo INTEGER NOT NULL DEFAULT 1,
                proxima_em TEXT NOT NULL,
                ultima_em TEXT,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TEXT
            );
            CREATE INDEX IF NOT EXISTS agendamento_busca_proxima
            ON agendamento_busca (ativo, proxima_em);
            CREATE TABLE IF NOT EXISTS arroba_descoberto (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criadora_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                plataforma TEXT NOT NULL DEFAULT '',
                arroba TEXT NOT NULL,
                origem TEXT NOT NULL DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (criadora_id, plataforma, arroba)
            );
            """
        )
        try:
            conn.execute(
                "ALTER TABLE instagram_post_hashes ADD COLUMN miniatura TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        _criar_tabelas_dominios(conn)
    _migrar_sites_oficiais()
    _migrar_usuarios()
    _migrar_criadoras()
    _migrar_busca_provedores()
    _migrar_busca_execucao()
    _semear_dominios_iniciais()


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
        CREATE TABLE IF NOT EXISTS perfil_monitorado (
            id BIGSERIAL PRIMARY KEY,
            criadora_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plataforma TEXT NOT NULL,
            arroba TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ativo',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (criadora_id, plataforma, arroba)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS nome_variante (
            id BIGSERIAL PRIMARY KEY,
            criadora_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            texto TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1,
            origem TEXT NOT NULL DEFAULT 'manual',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (criadora_id, texto)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pagina_oficial (
            id BIGSERIAL PRIMARY KEY,
            criadora_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            dominio TEXT NOT NULL DEFAULT '',
            origem TEXT NOT NULL DEFAULT 'manual',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (criadora_id, url)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS perfil_monitorado_criadora
        ON perfil_monitorado (criadora_id, status)
        """,
        """
        CREATE INDEX IF NOT EXISTS nome_variante_criadora
        ON nome_variante (criadora_id, ativo)
        """,
        """
        DROP TABLE IF EXISTS user_official_sites
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
        CREATE TABLE IF NOT EXISTS search_candidates (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            snippet TEXT NOT NULL DEFAULT '',
            thumbnail TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL,
            phash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, url)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS official_image_hashes (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            phash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, url)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS instagram_post_hashes (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            shortcode TEXT NOT NULL,
            url TEXT NOT NULL,
            miniatura TEXT NOT NULL DEFAULT '',
            phash TEXT NOT NULL,
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, shortcode)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS instagram_busca_controle (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ultima_busca_em TEXT NOT NULL,
            UNIQUE (user_id)
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
        """
        CREATE TABLE IF NOT EXISTS busca_execucao (
            id BIGSERIAL PRIMARY KEY,
            criadora_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provedor TEXT NOT NULL,
            termos_json JSONB NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pendente',
            erro_instagram TEXT,
            erro TEXT,
            resumo_json JSONB NOT NULL DEFAULT '{}',
            criada_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            concluida_em TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS busca_execucao_criadora
        ON busca_execucao (criadora_id, criada_em DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS agendamento_busca (
            id BIGSERIAL PRIMARY KEY,
            criadora_id BIGINT NOT NULL UNIQUE
                REFERENCES users(id) ON DELETE CASCADE,
            frequencia TEXT NOT NULL DEFAULT 'semanal',
            hora INTEGER NOT NULL DEFAULT 9,
            dia_semana INTEGER NOT NULL DEFAULT 1,
            ativo INTEGER NOT NULL DEFAULT 1,
            proxima_em TEXT NOT NULL,
            ultima_em TEXT,
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            atualizado_em TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS agendamento_busca_proxima
        ON agendamento_busca (ativo, proxima_em)
        """,
        """
        CREATE TABLE IF NOT EXISTS arroba_descoberto (
            id BIGSERIAL PRIMARY KEY,
            criadora_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plataforma TEXT NOT NULL DEFAULT '',
            arroba TEXT NOT NULL,
            origem TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (criadora_id, plataforma, arroba)
        )
        """,
    )
    with conexao() as conn:
        for comando in comandos:
            conn.execute(comando)
        _criar_tabelas_dominios(conn)


COLUNAS_BUSCA_PROVEDORES = {
    "execucao_id": "BIGINT",
    "url_normalizada": "TEXT NOT NULL DEFAULT ''",
    "dominio": "TEXT NOT NULL DEFAULT ''",
    "nota": "INTEGER NOT NULL DEFAULT 0",
    "motivos_json": "JSONB NOT NULL DEFAULT '[]'",
    "classificacao": "TEXT NOT NULL DEFAULT 'indefinido'",
    "primeira_vez_em": "TEXT",
    "ultima_vez_em": "TEXT",
    "resposta_bruta_json": "JSONB NOT NULL DEFAULT '{}'",
    "novo_para_criadora": "BOOLEAN NOT NULL DEFAULT TRUE",
    "chave_grupo": "TEXT NOT NULL DEFAULT ''",
    "consulta_log_id": "BIGINT",
    "posicao_busca": "INTEGER",
}
_COLUNAS_BUSCA_PROVEDORES_SQLITE = {
    "execucao_id": "INTEGER",
    "url_normalizada": "TEXT NOT NULL DEFAULT ''",
    "dominio": "TEXT NOT NULL DEFAULT ''",
    "nota": "INTEGER NOT NULL DEFAULT 0",
    "motivos_json": "TEXT NOT NULL DEFAULT '[]'",
    "classificacao": "TEXT NOT NULL DEFAULT 'indefinido'",
    "primeira_vez_em": "TEXT",
    "ultima_vez_em": "TEXT",
    "resposta_bruta_json": "TEXT NOT NULL DEFAULT '{}'",
    "novo_para_criadora": "INTEGER NOT NULL DEFAULT 1",
    "chave_grupo": "TEXT NOT NULL DEFAULT ''",
    "consulta_log_id": "INTEGER",
    "posicao_busca": "INTEGER",
}


def _migrar_busca_provedores():
    """Adiciona as colunas de pontuacao/classificacao em search_candidates.

    A tabela existente e reaproveitada: status, url unica por criadora e a tela
    de revisao continuam valendo. Nenhum dado salvo e apagado.
    """
    with conexao() as conn:
        if usando_postgresql():
            for coluna, tipo in COLUNAS_BUSCA_PROVEDORES.items():
                conn.execute(
                    f"ALTER TABLE search_candidates ADD COLUMN IF NOT EXISTS {coluna} {tipo}"
                )
        else:
            existentes = {
                coluna["name"]
                for coluna in conn.execute("PRAGMA table_info(search_candidates)")
            }
            for coluna, tipo in _COLUNAS_BUSCA_PROVEDORES_SQLITE.items():
                if coluna not in existentes:
                    conn.execute(
                        f"ALTER TABLE search_candidates ADD COLUMN {coluna} {tipo}"
                    )
        conn.execute(
            "UPDATE search_candidates SET url_normalizada = url"
            " WHERE url_normalizada IS NULL OR url_normalizada = ''"
        )
        conn.execute(
            "UPDATE search_candidates SET primeira_vez_em = created_at"
            " WHERE primeira_vez_em IS NULL"
        )
        conn.execute(
            "UPDATE search_candidates SET ultima_vez_em = created_at"
            " WHERE ultima_vez_em IS NULL"
        )
        _criar_tabela_consulta_log(conn)


COLUNAS_BUSCA_EXECUCAO = {
    "origem": "TEXT NOT NULL DEFAULT 'manual'",
    "agendamento_id": "BIGINT",
    "iniciada_em": "TEXT",
    "duracao_ms": "INTEGER",
    "motivo_falha": "TEXT NOT NULL DEFAULT ''",
}
_COLUNAS_BUSCA_EXECUCAO_SQLITE = {
    "origem": "TEXT NOT NULL DEFAULT 'manual'",
    "agendamento_id": "INTEGER",
    "iniciada_em": "TEXT",
    "duracao_ms": "INTEGER",
    "motivo_falha": "TEXT NOT NULL DEFAULT ''",
}


def _migrar_busca_execucao():
    """Colunas do agendamento na tabela de execucoes, sem apagar historico.

    As execucoes antigas ficam com origem 'manual' e sem duracao; nada e
    reescrito, so o que faltava passa a existir.
    """
    with conexao() as conn:
        if usando_postgresql():
            for coluna, tipo in COLUNAS_BUSCA_EXECUCAO.items():
                conn.execute(
                    f"ALTER TABLE busca_execucao ADD COLUMN IF NOT EXISTS {coluna} {tipo}"
                )
        else:
            existentes = {
                coluna["name"]
                for coluna in conn.execute("PRAGMA table_info(busca_execucao)")
            }
            for coluna, tipo in _COLUNAS_BUSCA_EXECUCAO_SQLITE.items():
                if coluna not in existentes:
                    conn.execute(
                        f"ALTER TABLE busca_execucao ADD COLUMN {coluna} {tipo}"
                    )


def _criar_tabela_consulta_log(conn):
    """Log de cada consulta executada: modelo, texto, parametros e posicoes."""
    id_execucao = "BIGSERIAL" if usando_postgresql() else "INTEGER"
    json_padrao = "'{}'" if usando_postgresql() else "'{}'"
    json_lista = "'[]'"
    if usando_postgresql():
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS consulta_log (
                id BIGSERIAL PRIMARY KEY,
                execucao_id BIGINT REFERENCES busca_execucao(id) ON DELETE CASCADE,
                modelo TEXT NOT NULL DEFAULT '',
                consulta_exata TEXT NOT NULL,
                parametros_json JSONB NOT NULL DEFAULT {json_padrao},
                posicao_do_candidato INTEGER,
                posicoes_json JSONB NOT NULL DEFAULT {json_lista},
                resposta_bruta_json JSONB NOT NULL DEFAULT {json_padrao},
                criada_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS consulta_log_execucao"
            " ON consulta_log (execucao_id, id)"
        )
        return
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS consulta_log (
            id {id_execucao} PRIMARY KEY AUTOINCREMENT,
            execucao_id INTEGER REFERENCES busca_execucao(id) ON DELETE CASCADE,
            modelo TEXT NOT NULL DEFAULT '',
            consulta_exata TEXT NOT NULL,
            parametros_json TEXT NOT NULL DEFAULT {json_padrao},
            posicao_do_candidato INTEGER,
            posicoes_json TEXT NOT NULL DEFAULT {json_lista},
            resposta_bruta_json TEXT NOT NULL DEFAULT {json_padrao},
            criada_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS consulta_log_execucao"
        " ON consulta_log (execucao_id, id)"
    )


TIPOS_DOMINIO = ("hospedagem_arquivos", "indice_albuns", "agregador_vazamento", "outro")
STATUS_DOMINIO = ("sugerido", "aprovado", "rejeitado")


def _criar_tabelas_dominios(conn):
    """Dominios suspeitos, ignorados e aparicoes por execucao.

    O registro por execucao (`dominio_execucao`) e o que permite contar
    execucoes/criadoras distintas sem expor quais criadoras viram o dominio.
    """
    tipo_json = "JSONB" if usando_postgresql() else "TEXT"
    id_execucao = "BIGINT" if usando_postgresql() else "INTEGER"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS dominio_suspeito (
            dominio TEXT PRIMARY KEY,
            tipo TEXT NOT NULL DEFAULT 'outro',
            status TEXT NOT NULL DEFAULT 'sugerido',
            origem TEXT NOT NULL DEFAULT 'auto',
            n_aparicoes INTEGER NOT NULL DEFAULT 0,
            n_execucoes_distintas INTEGER NOT NULL DEFAULT 0,
            n_criadoras_distintas INTEGER NOT NULL DEFAULT 0,
            n_confirmados INTEGER NOT NULL DEFAULT 0,
            n_falsos INTEGER NOT NULL DEFAULT 0,
            motivos {tipo_json} NOT NULL DEFAULT '{{}}',
            exemplo_url TEXT NOT NULL DEFAULT '',
            primeira_vez_em TEXT,
            ultima_vez_em TEXT,
            observacao TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS dominio_suspeito_status"
        " ON dominio_suspeito (status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dominio_ignorado (
            dominio TEXT PRIMARY KEY
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS dominio_execucao (
            dominio TEXT NOT NULL,
            execucao_id {id_execucao} NOT NULL,
            criadora_id {id_execucao},
            aparicoes INTEGER NOT NULL DEFAULT 0,
            nota INTEGER NOT NULL DEFAULT 0,
            criada_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (dominio, execucao_id)
        )
        """
    )


def _semear_dominios_iniciais():
    """Aplica o seed uma vez por boot; falha nunca impede a aplicacao de subir."""
    try:
        from app.core import dominios_suspeitos

        dominios_suspeitos.garantir_seed()
    except Exception:
        logger.exception("Falha ao semear a lista de dominios suspeitos")


def semear_dominios(itens, ignorados=()):
    """Insere seed/ignorados sem sobrescrever decisao humana (ON CONFLICT)."""
    agora = _agora_utc()
    with conexao() as conn:
        for item in itens or ():
            dominio = (item.get("dominio") or "").strip().lower()
            if not dominio:
                continue
            tipo = item.get("tipo") if item.get("tipo") in TIPOS_DOMINIO else "outro"
            status = item.get("status") if item.get("status") in STATUS_DOMINIO else "sugerido"
            conn.execute(
                """
                INSERT INTO dominio_suspeito
                    (dominio, tipo, status, origem, motivos, primeira_vez_em, ultima_vez_em)
                VALUES (?, ?, ?, 'seed', ?, ?, ?)
                ON CONFLICT (dominio) DO NOTHING
                """,
                (dominio, tipo, status, _valor_json({}), agora, agora),
            )
        for dominio in ignorados or ():
            dominio = (dominio or "").strip().lower()
            if not dominio:
                continue
            conn.execute(
                "INSERT INTO dominio_ignorado (dominio) VALUES (?)"
                " ON CONFLICT (dominio) DO NOTHING",
                (dominio,),
            )


def _dominio_com_motivos(linha) -> dict:
    item = dict(linha)
    motivos = item.get("motivos")
    if not isinstance(motivos, dict):
        try:
            motivos = json.loads(motivos or "{}")
        except (TypeError, ValueError):
            motivos = {}
    item["motivos"] = motivos if isinstance(motivos, dict) else {}
    item["nota"] = int(item["motivos"].get("nota") or 0)
    item["sinais"] = item["motivos"].get("sinais") or []
    return item


def obter_dominio_suspeito(dominio):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT dominio, tipo, status, origem, n_aparicoes,
                   n_execucoes_distintas, n_criadoras_distintas, n_confirmados,
                   n_falsos, motivos, exemplo_url, primeira_vez_em,
                   ultima_vez_em, observacao
            FROM dominio_suspeito WHERE dominio = ?
            """,
            ((dominio or "").strip().lower(),),
        ).fetchone()
    return _dominio_com_motivos(linha) if linha else None


def listar_dominios_suspeitos(status=None):
    consulta = """
        SELECT dominio, tipo, status, origem, n_aparicoes,
               n_execucoes_distintas, n_criadoras_distintas, n_confirmados,
               n_falsos, motivos, exemplo_url, primeira_vez_em,
               ultima_vez_em, observacao
        FROM dominio_suspeito
    """
    parametros = ()
    if status:
        consulta += " WHERE status = ?"
        parametros = (status,)
    consulta += " ORDER BY n_aparicoes DESC, dominio"
    with conexao() as conn:
        linhas = conn.execute(consulta, parametros).fetchall()
    return [_dominio_com_motivos(linha) for linha in linhas]


def dominios_aprovados(tipos=None):
    consulta = (
        "SELECT dominio, tipo FROM dominio_suspeito WHERE status = 'aprovado'"
    )
    parametros = ()
    if tipos:
        marcadores = ", ".join("?" for _ in tipos)
        consulta += f" AND tipo IN ({marcadores})"
        parametros = tuple(tipos)
    consulta += " ORDER BY dominio"
    with conexao() as conn:
        return [dict(linha) for linha in conn.execute(consulta, parametros).fetchall()]


def dominio_ignorado(dominio):
    with conexao() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM dominio_ignorado WHERE dominio = ?",
                ((dominio or "").strip().lower(),),
            ).fetchone()
            is not None
        )


def listar_dominios_ignorados():
    with conexao() as conn:
        return [
            linha["dominio"]
            for linha in conn.execute(
                "SELECT dominio FROM dominio_ignorado ORDER BY dominio"
            ).fetchall()
        ]


def registrar_execucao_dominio(dominio, execucao_id, criadora_id, aparicoes, nota):
    """Guarda (uma vez por execucao) que o dominio apareceu, para as contagens."""
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO dominio_execucao
                (dominio, execucao_id, criadora_id, aparicoes, nota, criada_em)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (dominio, execucao_id) DO UPDATE SET
                criadora_id = excluded.criadora_id,
                aparicoes = excluded.aparicoes,
                nota = excluded.nota
            """,
            (
                (dominio or "").strip().lower(),
                execucao_id,
                criadora_id,
                max(0, int(aparicoes or 0)),
                int(nota or 0),
                _agora_utc(),
            ),
        )


def contagens_do_dominio(dominio):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT COUNT(DISTINCT execucao_id) AS execucoes,
                   COUNT(DISTINCT criadora_id) AS criadoras,
                   COALESCE(SUM(aparicoes), 0) AS aparicoes
            FROM dominio_execucao WHERE dominio = ?
            """,
            ((dominio or "").strip().lower(),),
        ).fetchone()
    return {
        "execucoes": int(linha["execucoes"] or 0),
        "criadoras": int(linha["criadoras"] or 0),
        "aparicoes": int(linha["aparicoes"] or 0),
    }


def registrar_sugestao_dominio(
    dominio, nota, sinais, exemplo_url, contagens, observacao=""
):
    """Cria ou atualiza a sugestao. Nunca mexe em aprovado/rejeitado."""
    dominio = (dominio or "").strip().lower()
    if not dominio:
        return False
    agora = _agora_utc()
    motivos = _valor_json({"nota": int(nota or 0), "sinais": list(sinais or [])})
    with conexao() as conn:
        existente = conn.execute(
            "SELECT status FROM dominio_suspeito WHERE dominio = ?", (dominio,)
        ).fetchone()
        if existente is None:
            conn.execute(
                """
                INSERT INTO dominio_suspeito
                    (dominio, tipo, status, origem, n_aparicoes,
                     n_execucoes_distintas, n_criadoras_distintas, motivos,
                     exemplo_url, primeira_vez_em, ultima_vez_em, observacao)
                VALUES (?, 'outro', 'sugerido', 'auto', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dominio,
                    int(contagens.get("aparicoes") or 0),
                    int(contagens.get("execucoes") or 0),
                    int(contagens.get("criadoras") or 0),
                    motivos,
                    (exemplo_url or "")[:1000],
                    agora,
                    agora,
                    (observacao or "")[:500],
                ),
            )
            return True
        if existente["status"] != "sugerido":
            return False
        conn.execute(
            """
            UPDATE dominio_suspeito SET
                n_aparicoes = ?, n_execucoes_distintas = ?,
                n_criadoras_distintas = ?, motivos = ?,
                exemplo_url = CASE WHEN exemplo_url = '' THEN ? ELSE exemplo_url END,
                ultima_vez_em = ?,
                observacao = CASE WHEN observacao = '' THEN ? ELSE observacao END
            WHERE dominio = ? AND status = 'sugerido'
            """,
            (
                int(contagens.get("aparicoes") or 0),
                int(contagens.get("execucoes") or 0),
                int(contagens.get("criadoras") or 0),
                motivos,
                (exemplo_url or "")[:1000],
                agora,
                (observacao or "")[:500],
                dominio,
            ),
        )
        return True


def atualizar_status_dominio(dominio, status, tipo=None, observacao=None):
    if status not in STATUS_DOMINIO:
        raise ValueError("Status de dominio invalido.")
    campos = ["status = ?", "origem = 'revisao'"]
    parametros = [status]
    if tipo:
        if tipo not in TIPOS_DOMINIO:
            raise ValueError("Tipo de dominio invalido.")
        campos.append("tipo = ?")
        parametros.append(tipo)
    if observacao is not None:
        campos.append("observacao = ?")
        parametros.append(observacao[:500])
    parametros.append((dominio or "").strip().lower())
    with conexao() as conn:
        cur = conn.execute(
            f"UPDATE dominio_suspeito SET {', '.join(campos)} WHERE dominio = ?",
            tuple(parametros),
        )
        return cur.rowcount > 0


def registrar_feedback_dominio(dominio, confirmado, limite=10):
    """Soma confirmado/falso e marca para revisao quando os falsos dominam."""
    dominio = (dominio or "").strip().lower()
    campo = "n_confirmados" if confirmado else "n_falsos"
    with conexao() as conn:
        cur = conn.execute(
            f"UPDATE dominio_suspeito SET {campo} = {campo} + 1 WHERE dominio = ?",
            (dominio,),
        )
        if cur.rowcount == 0:
            return False
        linha = conn.execute(
            "SELECT status, n_confirmados, n_falsos FROM dominio_suspeito"
            " WHERE dominio = ?",
            (dominio,),
        ).fetchone()
        if (
            linha["status"] == "aprovado"
            and int(linha["n_confirmados"] or 0) + int(linha["n_falsos"] or 0) >= limite
            and int(linha["n_falsos"] or 0) > int(linha["n_confirmados"] or 0)
        ):
            conn.execute(
                "UPDATE dominio_suspeito SET observacao = 'revisar' WHERE dominio = ?",
                (dominio,),
            )
        return True


def listar_candidatos_da_execucao(execucao_id):
    with conexao() as conn:
        return [
            dict(linha)
            for linha in conn.execute(
                """
                SELECT url, dominio, title AS titulo, snippet AS trecho
                FROM search_candidates WHERE execucao_id = ?
                """,
                (execucao_id,),
            ).fetchall()
        ]


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
        if conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            return False
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
            SELECT id, username, email, email_verified, role, public_name,
                   public_handle, created_at
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
        _bloquear_usuarios(conn)
        _verificar_admin_restante(conn, user_id, role)
        cur = conn.execute(
            "UPDATE users SET role = ? WHERE id = ?",
            (role, user_id),
        )
        return cur.rowcount > 0


def _bloquear_usuarios(conn):
    if usando_postgresql():
        conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
    else:
        conn.execute("BEGIN IMMEDIATE")


def _verificar_admin_restante(conn, user_id, nova_role):
    usuario = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if usuario and usuario["role"] == "admin" and nova_role != "admin":
        outro = conn.execute(
            "SELECT id FROM users WHERE role = 'admin' AND id != ? LIMIT 1",
            (user_id,),
        ).fetchone()
        if not outro:
            raise ValueError("É necessário manter pelo menos um administrador.")


def editar_usuario(user_id, username, email, public_name, public_handle, role,
                   password_hash=None):
    if role not in {"admin", "cliente"}:
        raise ValueError("Perfil de usuário inválido.")
    with conexao() as conn:
        _bloquear_usuarios(conn)
        usuario = conn.execute(
            "SELECT email FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not usuario:
            return None
        _verificar_admin_restante(conn, user_id, role)
        email_alterado = usuario["email"] != email
        conn.execute(
            """
            UPDATE users SET username = ?, email = ?, public_name = ?,
                public_handle = ?, role = ?,
                email_verified = CASE WHEN ? THEN 0 ELSE email_verified END,
                password_hash = COALESCE(?, password_hash)
            WHERE id = ?
            """,
            (username, email, public_name, public_handle, role, email_alterado,
             password_hash, user_id),
        )
        conn.execute(
            """
            UPDATE criadoras SET
                nome = CASE WHEN EXISTS (
                    SELECT 1 FROM criadoras outra
                    WHERE outra.nome = ? AND outra.id != criadoras.id
                ) THEN nome ELSE ? END,
                handle = ?, email = ?
            WHERE user_id = ?
            """,
            (public_name, public_name, public_handle, email, user_id),
        )
        if email_alterado:
            conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
        if email_alterado or password_hash:
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        return email_alterado


def excluir_usuario(user_id):
    with conexao() as conn:
        _bloquear_usuarios(conn)
        if not conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
            return None
        _verificar_admin_restante(conn, user_id, None)
        fotos = [linha["arquivo"] for linha in conn.execute(
            "SELECT arquivo FROM user_search_photos WHERE user_id = ?", (user_id,)
        ).fetchall()]
        arquivos = [linha["arquivo"] for linha in conn.execute(
            """
            SELECT DISTINCT c.arquivo FROM conteudos c
            JOIN criadoras cr ON cr.id = c.criadora_id
            WHERE cr.user_id = ? AND NOT EXISTS (
                SELECT 1 FROM conteudos outro
                JOIN criadoras dona ON dona.id = outro.criadora_id
                WHERE outro.arquivo = c.arquivo
                  AND (dona.user_id IS NULL OR dona.user_id != ?)
            )
            """, (user_id, user_id)
        ).fetchall()]
        conn.execute(
            """DELETE FROM hashes WHERE conteudo_id IN (
                SELECT c.id FROM conteudos c JOIN criadoras cr ON cr.id = c.criadora_id
                WHERE cr.user_id = ?
            )""", (user_id,)
        )
        conn.execute("DELETE FROM conteudos WHERE criadora_id IN (SELECT id FROM criadoras WHERE user_id = ?)", (user_id,))
        conn.execute("DELETE FROM sites_oficiais WHERE criadora_id IN (SELECT id FROM criadoras WHERE user_id = ?)", (user_id,))
        conn.execute("DELETE FROM criadoras WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE leak_reports SET reported_by = user_id WHERE reported_by = ? AND user_id != ?", (user_id, user_id))
        for snapshot in conn.execute(
            "SELECT user_id, payload_json FROM search_snapshots WHERE kind = 'busca' AND user_id != ?",
            (user_id,),
        ).fetchall():
            try:
                dados = json.loads(snapshot["payload_json"])
            except ValueError:
                logger.warning("Snapshot de busca inválido para usuário %s", snapshot["user_id"])
                continue
            if isinstance(dados, dict) and dados.get("target_user_id") == user_id:
                conn.execute(
                    "DELETE FROM search_snapshots WHERE user_id = ? AND kind = 'busca'",
                    (snapshot["user_id"],),
                )
        conn.execute("DELETE FROM app_settings WHERE name = ?", (f"vazamento_status_{user_id}",))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return fotos, arquivos


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
            UPDATE criadoras
            SET nome = CASE
                WHEN EXISTS (SELECT 1 FROM criadoras outra
                             WHERE outra.nome = ? AND outra.id != criadoras.id)
                THEN nome ELSE ? END,
                handle = ?
            WHERE user_id = ?
            """,
            (public_name, public_name, public_handle, user_id),
        )
        return atualizado


def listar_usuarios_para_busca():
    """Lista contas, deixando as sem perfil de busca por último."""
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, username, public_name, public_handle
            FROM users
            ORDER BY (public_name IS NULL OR public_handle IS NULL),
                     LOWER(COALESCE(public_name, username)), username
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


LIMITE_PERFIS_PADRAO = 5
STATUS_PERFIL_MONITORADO = {"ativo", "pausado"}


def limite_perfis_monitorados() -> int:
    """Quantos perfis uma conta pode monitorar.

    Precedencia: variavel de ambiente, depois app_settings (editavel sem
    deploy), depois o padrao do codigo.
    """
    do_ambiente = os.getenv("PERFIS_MONITORADOS_LIMITE", "").strip()
    if do_ambiente:
        try:
            return max(1, int(do_ambiente))
        except ValueError:
            pass
    try:
        return max(
            1,
            int(
                obter_configuracao(
                    "perfis_monitorados_limite", str(LIMITE_PERFIS_PADRAO)
                )
            ),
        )
    except (TypeError, ValueError):
        return LIMITE_PERFIS_PADRAO


def listar_perfis_monitorados(criadora_id, status=None):
    sql = (
        "SELECT id, criadora_id, plataforma, arroba, status, criado_em"
        " FROM perfil_monitorado WHERE criadora_id = ?"
    )
    parametros = [criadora_id]
    if status:
        sql += " AND status = ?"
        parametros.append(status)
    sql += " ORDER BY plataforma, arroba"
    with conexao() as conn:
        return conn.execute(sql, parametros).fetchall()


def obter_perfil_monitorado(perfil_id, criadora_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, criadora_id, plataforma, arroba, status
            FROM perfil_monitorado
            WHERE id = ? AND criadora_id = ?
            """,
            (perfil_id, criadora_id),
        ).fetchone()


def adicionar_perfil_monitorado(criadora_id, plataforma, arroba, status="ativo"):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO perfil_monitorado (criadora_id, plataforma, arroba, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (criadora_id, plataforma, arroba) DO NOTHING
            RETURNING id
            """,
            (criadora_id, plataforma, arroba, status),
        )
        registro = cur.fetchone()
        return registro["id"] if registro else None


def atualizar_status_perfil_monitorado(perfil_id, criadora_id, status):
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE perfil_monitorado SET status = ?
            WHERE id = ? AND criadora_id = ?
            """,
            (status, perfil_id, criadora_id),
        )
        return cur.rowcount > 0


def excluir_perfil_monitorado(perfil_id, criadora_id):
    with conexao() as conn:
        cur = conn.execute(
            "DELETE FROM perfil_monitorado WHERE id = ? AND criadora_id = ?",
            (perfil_id, criadora_id),
        )
        return cur.rowcount > 0


def listar_variantes_nome(criadora_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, criadora_id, texto, ativo, origem, criado_em
            FROM nome_variante
            WHERE criadora_id = ?
            ORDER BY ativo DESC, texto
            """,
            (criadora_id,),
        ).fetchall()


def adicionar_variante_nome(criadora_id, texto, origem="manual", ativo=True):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO nome_variante (criadora_id, texto, ativo, origem)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (criadora_id, texto) DO NOTHING
            RETURNING id
            """,
            (criadora_id, texto, 1 if ativo else 0, origem),
        )
        registro = cur.fetchone()
        return registro["id"] if registro else None


def atualizar_variante_nome(variante_id, criadora_id, ativo):
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE nome_variante SET ativo = ?
            WHERE id = ? AND criadora_id = ?
            """,
            (1 if ativo else 0, variante_id, criadora_id),
        )
        return cur.rowcount > 0


def excluir_variante_nome(variante_id, criadora_id):
    with conexao() as conn:
        cur = conn.execute(
            "DELETE FROM nome_variante WHERE id = ? AND criadora_id = ?",
            (variante_id, criadora_id),
        )
        return cur.rowcount > 0


def listar_paginas_oficiais(criadora_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, criadora_id, url, dominio, origem, criado_em
            FROM pagina_oficial
            WHERE criadora_id = ?
            ORDER BY dominio, url
            """,
            (criadora_id,),
        ).fetchall()


def adicionar_pagina_oficial(criadora_id, url, dominio, origem="manual"):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO pagina_oficial (criadora_id, url, dominio, origem)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (criadora_id, url) DO NOTHING
            RETURNING id
            """,
            (criadora_id, url, dominio, origem),
        )
        registro = cur.fetchone()
        return registro["id"] if registro else None


def excluir_pagina_oficial(pagina_id, criadora_id):
    with conexao() as conn:
        cur = conn.execute(
            "DELETE FROM pagina_oficial WHERE id = ? AND criadora_id = ?",
            (pagina_id, criadora_id),
        )
        return cur.rowcount > 0


STATUS_CANDIDATO = {"pending", "leaked", "not_me", "official"}


def adicionar_candidato(
    user_id, url, title, source, snippet, thumbnail, engine, phash
):
    agora = _agora_utc()
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO search_candidates (
                user_id, url, title, source, snippet, thumbnail, engine, phash,
                url_normalizada, primeira_vez_em, ultima_vez_em
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, url) DO NOTHING
            RETURNING id
            """,
            (
                user_id, url, title, source, snippet, thumbnail, engine, phash,
                url, agora, agora,
            ),
        )
        registro = cur.fetchone()
        return registro["id"] if registro else None


def listar_candidatos(user_id, status=None):
    consulta = """
        SELECT id, user_id, url, title, source, snippet, thumbnail, engine,
               phash, status, created_at, execucao_id, url_normalizada, dominio,
               nota, motivos_json, classificacao, primeira_vez_em, ultima_vez_em,
               novo_para_criadora, chave_grupo, consulta_log_id, posicao_busca,
               (SELECT modelo FROM consulta_log
                 WHERE id = search_candidates.consulta_log_id) AS consulta_modelo,
               (SELECT consulta_exata FROM consulta_log
                 WHERE id = search_candidates.consulta_log_id) AS consulta_texto
        FROM search_candidates
        WHERE user_id = ?
    """
    parametros = [user_id]
    if status:
        consulta += " AND status = ?"
        parametros.append(status)
    consulta += " ORDER BY created_at DESC, id DESC"
    with conexao() as conn:
        return conn.execute(consulta, tuple(parametros)).fetchall()


def listar_candidatos_revisao(user_id, status=None, nota_minima=None):
    """Candidatos ordenados por nota, com os novos primeiro no empate."""
    consulta = """
        SELECT id, user_id, url, title, source, snippet, thumbnail, engine,
               phash, status, created_at, execucao_id, url_normalizada, dominio,
               nota, motivos_json, classificacao, primeira_vez_em, ultima_vez_em,
               novo_para_criadora, chave_grupo, consulta_log_id, posicao_busca,
               (SELECT modelo FROM consulta_log
                 WHERE id = search_candidates.consulta_log_id) AS consulta_modelo,
               (SELECT consulta_exata FROM consulta_log
                 WHERE id = search_candidates.consulta_log_id) AS consulta_texto
        FROM search_candidates
        WHERE user_id = ?
    """
    parametros = [user_id]
    if status:
        consulta += " AND status = ?"
        parametros.append(status)
    if nota_minima is not None:
        consulta += " AND nota >= ?"
        parametros.append(nota_minima)
    consulta += (
        " ORDER BY nota DESC, novo_para_criadora DESC,"
        " COALESCE(ultima_vez_em, created_at) DESC, id DESC"
    )
    with conexao() as conn:
        return conn.execute(consulta, tuple(parametros)).fetchall()


def urls_conhecidas(user_id):
    with conexao() as conn:
        return {
            linha["url"]
            for linha in conn.execute(
                "SELECT url FROM search_candidates WHERE user_id = ?", (user_id,)
            ).fetchall()
        }


def criar_busca_execucao(
    criadora_id,
    provedor,
    termos,
    *,
    status="rodando",
    origem="manual",
    agendamento_id=None,
):
    agora = _agora_utc()
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO busca_execucao
                (criadora_id, provedor, termos_json, status, origem,
                 agendamento_id, criada_em, iniciada_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                criadora_id,
                provedor,
                _valor_json(list(termos)),
                status,
                origem,
                agendamento_id,
                agora,
                agora if status == "rodando" else None,
            ),
        )
        return cur.fetchone()["id"]


def reservar_execucao(execucao_id):
    """Poe a execucao em 'rodando' de forma atomica.

    Falso quando outro worker (ou outra tentativa nossa) ja pegou a linha: e a
    trava que impede a mesma execucao rodar duas vezes depois de um reinicio.
    """
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE busca_execucao
               SET status = 'rodando', iniciada_em = ?
             WHERE id = ? AND status = 'na_fila'
            """,
            (_agora_utc(), execucao_id),
        )
        return cur.rowcount > 0


def finalizar_busca_execucao(
    execucao_id,
    *,
    status,
    resumo=None,
    erro="",
    motivo_falha="",
    erro_instagram=None,
):
    campos = [
        "status = ?",
        "resumo_json = ?",
        "erro = ?",
        "motivo_falha = ?",
        "concluida_em = ?",
    ]
    parametros = [
        status,
        _valor_json(resumo or {}),
        (erro or "")[:2000],
        (motivo_falha or "")[:40],
        _agora_utc(),
    ]
    if erro_instagram is not None:
        campos.append("erro_instagram = ?")
        parametros.append((erro_instagram or "")[:2000])
    parametros.append(execucao_id)
    with conexao() as conn:
        conn.execute(
            f"UPDATE busca_execucao SET {', '.join(campos)} WHERE id = ?",
            tuple(parametros),
        )


def registrar_duracao_execucao(execucao_id, duracao_ms):
    with conexao() as conn:
        conn.execute(
            "UPDATE busca_execucao SET duracao_ms = ? WHERE id = ?",
            (int(max(0, duracao_ms)), execucao_id),
        )


def execucao_em_andamento(criadora_id):
    """Execucao que ainda vai rodar ou esta rodando agora."""
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT id, status, origem FROM busca_execucao
            WHERE criadora_id = ? AND status IN ('na_fila', 'rodando')
            ORDER BY id DESC LIMIT 1
            """,
            (criadora_id,),
        ).fetchone()
    return dict(linha) if linha else None


def contar_execucoes_desde(criadora_id, desde_iso):
    """Buscas desta criadora desde o inicio do dia local (qualquer status).

    Falha conta como tentativa: o limite diario tambem protege contra um erro
    que se repete a cada clique.
    """
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT COUNT(*) AS total FROM busca_execucao
            WHERE criadora_id = ? AND criada_em >= ?
            """,
            (criadora_id, desde_iso),
        ).fetchone()
    return int((linha["total"] if linha else 0) or 0)


def _execucao_com_resumo(registro):
    registro = dict(registro)
    valor = registro.get("resumo_json")
    if not isinstance(valor, (dict, list)):
        try:
            registro["resumo_json"] = json.loads(valor or "{}")
        except (TypeError, ValueError):
            registro["resumo_json"] = {}
    return registro


def ultima_execucao(criadora_id):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT id, criadora_id, provedor, status, origem, erro, motivo_falha,
                   erro_instagram, resumo_json, criada_em, iniciada_em,
                   concluida_em, duracao_ms
            FROM busca_execucao
            WHERE criadora_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (criadora_id,),
        ).fetchone()
    return _execucao_com_resumo(linha) if linha else None


def listar_execucoes_criadora(criadora_id, limite=30):
    """Historico do agendamento: data, provedor, buscas, novos, duracao, erro."""
    with conexao() as conn:
        linhas = conn.execute(
            """
            SELECT id, provedor, status, origem, erro, motivo_falha,
                   erro_instagram, resumo_json, criada_em, iniciada_em,
                   concluida_em, duracao_ms
            FROM busca_execucao
            WHERE criadora_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (criadora_id, int(limite)),
        ).fetchall()
    return [_execucao_com_resumo(linha) for linha in linhas]


def marcar_execucoes_abandonadas(minutos=60):
    """Fecha execucoes que ficaram presas em na_fila/rodando.

    Acontece quando o container morre no meio; sem isso a criadora ficaria
    bloqueada por uma execucao que nunca termina. O motivo deixa claro que nao
    foi o provedor, e sim uma interrupcao.
    """
    limite = datetime.now(timezone.utc) - timedelta(minutes=int(minutos))
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE busca_execucao
               SET status = 'erro', motivo_falha = 'interrompida',
                   erro = 'A execucao foi interrompida antes de terminar.',
                   concluida_em = ?
             WHERE status IN ('na_fila', 'rodando')
               AND COALESCE(iniciada_em, criada_em) < ?
            """,
            (_agora_utc(), limite.isoformat()),
        )
        return cur.rowcount


def atualizar_busca_execucao(
    execucao_id, *, status=None, erro_instagram=None, erro=None, resumo=None
):
    campos = []
    parametros = []
    if status is not None:
        campos.append("status = ?")
        parametros.append(status)
        if status in {"concluida", "erro"}:
            campos.append("concluida_em = ?")
            parametros.append(_agora_utc())
    if erro_instagram is not None:
        campos.append("erro_instagram = ?")
        parametros.append(erro_instagram[:2000])
    if erro is not None:
        campos.append("erro = ?")
        parametros.append(erro[:2000])
    if resumo is not None:
        campos.append("resumo_json = ?")
        parametros.append(_valor_json(resumo))
    if not campos:
        return False
    parametros.append(execucao_id)
    with conexao() as conn:
        cur = conn.execute(
            f"UPDATE busca_execucao SET {', '.join(campos)} WHERE id = ?",
            tuple(parametros),
        )
        return cur.rowcount > 0


def obter_busca_execucao(execucao_id):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT id, criadora_id, provedor, termos_json, status, origem,
                   agendamento_id, iniciada_em, duracao_ms, motivo_falha,
                   erro_instagram, erro, resumo_json, criada_em, concluida_em
            FROM busca_execucao WHERE id = ?
            """,
            (execucao_id,),
        ).fetchone()
    if not linha:
        return None
    registro = dict(linha)
    for chave in ("termos_json", "resumo_json"):
        valor = registro.get(chave)
        if isinstance(valor, (dict, list)):
            continue
        try:
            registro[chave] = json.loads(valor or "{}")
        except (TypeError, ValueError):
            registro[chave] = [] if chave == "termos_json" else {}
    return registro


# ------------------------------------------------------------- agendamento

LIMITE_BUSCAS_DIA_PADRAO = 4


def limite_buscas_por_dia() -> int:
    """Buscas diarias por criadora: ambiente, depois app_settings, depois 4."""
    do_ambiente = os.getenv("BUSCAS_POR_DIA_LIMITE", "").strip()
    if do_ambiente:
        try:
            return max(1, int(do_ambiente))
        except ValueError:
            pass
    valor = obter_configuracao("buscas_por_dia_limite", "").strip()
    if valor:
        try:
            return max(1, int(valor))
        except ValueError:
            pass
    return LIMITE_BUSCAS_DIA_PADRAO


def salvar_agendamento_busca(
    criadora_id, frequencia, hora, dia_semana, proxima_em, ativo=1
):
    agora = _agora_utc()
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO agendamento_busca
                (criadora_id, frequencia, hora, dia_semana, ativo, proxima_em,
                 criado_em, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (criadora_id) DO UPDATE SET
                frequencia = excluded.frequencia,
                hora = excluded.hora,
                dia_semana = excluded.dia_semana,
                ativo = excluded.ativo,
                proxima_em = excluded.proxima_em,
                atualizado_em = excluded.atualizado_em
            """,
            (
                criadora_id,
                frequencia,
                int(hora),
                int(dia_semana),
                int(ativo),
                proxima_em,
                agora,
                agora,
            ),
        )


def obter_agendamento_busca(criadora_id):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT id, criadora_id, frequencia, hora, dia_semana, ativo,
                   proxima_em, ultima_em, criado_em, atualizado_em
            FROM agendamento_busca WHERE criadora_id = ?
            """,
            (criadora_id,),
        ).fetchone()
    return dict(linha) if linha else None


def desativar_agendamento_busca(criadora_id):
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE agendamento_busca SET ativo = 0, atualizado_em = ?
            WHERE criadora_id = ?
            """,
            (_agora_utc(), criadora_id),
        )
        return cur.rowcount > 0


def listar_agendamentos_vencidos(agora_iso, limite=20):
    """Agendamentos ativos cuja hora ja passou, com a criadora junto."""
    with conexao() as conn:
        linhas = conn.execute(
            """
            SELECT a.id AS agendamento_id, a.criadora_id, a.frequencia, a.hora,
                   a.dia_semana, a.proxima_em, u.username, u.public_name,
                   u.public_handle
            FROM agendamento_busca a
            JOIN users u ON u.id = a.criadora_id
            WHERE a.ativo = 1 AND a.proxima_em <= ?
            ORDER BY a.proxima_em
            LIMIT ?
            """,
            (agora_iso, int(limite)),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def reservar_agendamento_busca(agendamento_id, proxima_esperada, proxima_nova):
    """Avanca a proxima execucao so se ninguem avancou antes.

    Devolve True para exatamente um chamador. E o que impede dois ticks do
    scheduler (ou um reinicio no meio) de enfileirarem a mesma busca duas vezes.
    """
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE agendamento_busca
               SET proxima_em = ?, ultima_em = ?, atualizado_em = ?
             WHERE id = ? AND proxima_em = ? AND ativo = 1
            """,
            (proxima_nova, _agora_utc(), _agora_utc(), agendamento_id, proxima_esperada),
        )
        return cur.rowcount > 0


def registrar_consulta_log(
    execucao_id, modelo, consulta, parametros=None, posicoes=None, resposta_bruta=None
):
    """Grava a consulta executada com o que ela usou e onde cada resultado veio.

    `posicoes` e a lista [{"url": ..., "posicao": 1}, ...] na ordem em que o
    provedor devolveu. O candidato guarda o id deste log em `consulta_log_id`.
    """
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO consulta_log (
                execucao_id, modelo, consulta_exata, parametros_json,
                posicoes_json, resposta_bruta_json, criada_em
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                execucao_id,
                (modelo or "")[:40],
                (consulta or "")[:1000],
                _valor_json(parametros or {}),
                _valor_json(posicoes or []),
                _valor_json(resposta_bruta or {}),
                _agora_utc(),
            ),
        )
        return cur.fetchone()["id"]


def listar_consulta_log(execucao_id):
    """Historico das consultas de uma execucao, na ordem em que rodaram."""
    with conexao() as conn:
        return [
            dict(linha)
            for linha in conn.execute(
                """
                SELECT id, execucao_id, modelo, consulta_exata, parametros_json,
                       posicao_do_candidato, posicoes_json, criada_em
                FROM consulta_log WHERE execucao_id = ? ORDER BY id
                """,
                (execucao_id,),
            ).fetchall()
        ]


def salvar_arroba_descoberto(criadora_id, plataforma, arroba, origem=""):
    arroba = (arroba or "").strip().lstrip("@").lower()
    if not arroba:
        return False
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO arroba_descoberto
                (criadora_id, plataforma, arroba, origem)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (criadora_id, plataforma, arroba) DO UPDATE
            SET origem = excluded.origem
            """,
            (criadora_id, (plataforma or "").strip().lower(), arroba, origem[:500]),
        )
        return True


def listar_arrobas_descobertos(criadora_id):
    with conexao() as conn:
        return [
            dict(linha)
            for linha in conn.execute(
                """
                SELECT arroba, plataforma, origem
                FROM arroba_descoberto WHERE criadora_id = ?
                ORDER BY id
                """,
                (criadora_id,),
            ).fetchall()
        ]


def salvar_candidatos_busca(criadora_id, execucao_id, candidatos):
    """Grava/atualiza candidatos sem tocar no status de revisao existente.

    Cada item traz url_normalizada, dominio, titulo, trecho, miniatura_url,
    provedor, nota, motivos, classificacao e resposta_bruta. Devolve
    (novos, atualizados).
    """
    if not candidatos:
        return 0, 0
    conhecidas = urls_conhecidas(criadora_id)
    novos = atualizados = 0
    agora = _agora_utc()
    with conexao() as conn:
        for item in candidatos:
            url = item["url_normalizada"]
            try:
                conn.execute(
                    """
                    INSERT INTO search_candidates (
                        user_id, url, title, source, snippet, thumbnail, engine,
                        phash, status, created_at, execucao_id, url_normalizada,
                        dominio, nota, motivos_json, classificacao,
                        primeira_vez_em, ultima_vez_em, resposta_bruta_json,
                        novo_para_criadora, chave_grupo, consulta_log_id,
                        posicao_busca
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, '', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, url) DO UPDATE SET
                        title = excluded.title,
                        source = excluded.source,
                        snippet = excluded.snippet,
                        thumbnail = excluded.thumbnail,
                        engine = excluded.engine,
                        execucao_id = excluded.execucao_id,
                        url_normalizada = excluded.url_normalizada,
                        dominio = excluded.dominio,
                        nota = excluded.nota,
                        motivos_json = excluded.motivos_json,
                        classificacao = excluded.classificacao,
                        ultima_vez_em = excluded.ultima_vez_em,
                        resposta_bruta_json = excluded.resposta_bruta_json,
                        novo_para_criadora = ?,
                        chave_grupo = excluded.chave_grupo,
                        consulta_log_id = excluded.consulta_log_id,
                        posicao_busca = excluded.posicao_busca
                    """,
                    (
                        criadora_id,
                        url,
                        (item.get("titulo") or "Resultado")[:500],
                        (item.get("origem") or item.get("dominio") or "")[:200],
                        (item.get("trecho") or "")[:4000],
                        item.get("miniatura_url") or "",
                        item.get("provedor") or "",
                        agora,
                        execucao_id,
                        url,
                        item.get("dominio") or "",
                        int(item.get("nota") or 0),
                        _valor_json(item.get("motivos") or []),
                        item.get("classificacao") or "indefinido",
                        agora,
                        agora,
                        _valor_json(item.get("resposta_bruta") or {}),
                        True,
                        item.get("chave_grupo") or "",
                        item.get("consulta_log_id"),
                        item.get("posicao_busca"),
                        False,
                    ),
                )
            except Exception:
                logger.exception("Falha ao salvar candidato %s", url)
                continue
            log_id = item.get("consulta_log_id")
            posicao = item.get("posicao_busca")
            if log_id and posicao:
                conn.execute(
                    "UPDATE consulta_log SET posicao_do_candidato = ?"
                    " WHERE id = ? AND (posicao_do_candidato IS NULL"
                    " OR posicao_do_candidato > ?)",
                    (posicao, log_id, posicao),
                )
            if url in conhecidas:
                atualizados += 1
            else:
                novos += 1
                conhecidas.add(url)
    return novos, atualizados


def contar_candidatos_por_status(user_id):
    with conexao() as conn:
        linhas = conn.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM search_candidates WHERE user_id = ?
            GROUP BY status
            """,
            (user_id,),
        ).fetchall()
    return {linha["status"]: linha["total"] for linha in linhas}


def contar_candidatos_abaixo_do_limiar(user_id, limiar):
    """Pendentes que ficaram abaixo da nota minima e nao entram na fila."""
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT COUNT(*) AS total FROM search_candidates
            WHERE user_id = ? AND status = 'pending' AND COALESCE(nota, 0) < ?
            """,
            (user_id, int(limiar)),
        ).fetchone()
    return int((linha["total"] if linha else 0) or 0)


def _lista_json(valor):
    """Lista vinda de JSONB (PostgreSQL) ou de texto JSON (SQLite de teste)."""
    if isinstance(valor, list):
        return valor
    try:
        dados = json.loads(valor or "[]")
    except (TypeError, ValueError):
        return []
    return dados if isinstance(dados, list) else []


def listar_execucoes(limite=30):
    """Execucoes de busca com as consultas de cada uma, mais recentes primeiro.

    Cada consulta traz quantos resultados o provedor devolveu e quantos
    candidatos ficaram na Valida, para saber qual modelo achou o que.
    """
    with conexao() as conn:
        execucoes = conn.execute(
            """
            SELECT execucao_id, MIN(criada_em) AS inicio, MAX(criada_em) AS fim
            FROM consulta_log
            WHERE execucao_id IS NOT NULL
            GROUP BY execucao_id
            ORDER BY MAX(id) DESC
            LIMIT ?
            """,
            (int(limite),),
        ).fetchall()
        resultado = []
        for execucao in execucoes:
            linhas = conn.execute(
                """
                SELECT c.id, c.modelo, c.consulta_exata, c.criada_em, c.posicoes_json,
                       (SELECT COUNT(*) FROM search_candidates s
                         WHERE s.consulta_log_id = c.id) AS candidatos
                FROM consulta_log c
                WHERE c.execucao_id = ?
                ORDER BY c.id
                """,
                (execucao["execucao_id"],),
            ).fetchall()
            consultas = []
            for linha in linhas:
                consultas.append(
                    {
                        "id": linha["id"],
                        "modelo": (linha["modelo"] or "").strip(),
                        "consulta": linha["consulta_exata"],
                        "criada_em": linha["criada_em"],
                        "resultados": len(_lista_json(linha["posicoes_json"])),
                        "candidatos": int(linha["candidatos"] or 0),
                    }
                )
            resultado.append(
                {
                    "execucao_id": execucao["execucao_id"],
                    "inicio": execucao["inicio"],
                    "fim": execucao["fim"],
                    "consultas": consultas,
                    "resultados": sum(item["resultados"] for item in consultas),
                    "candidatos": sum(item["candidatos"] for item in consultas),
                }
            )
    return resultado


def obter_candidato(candidate_id, user_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, user_id, url, title, source, snippet, thumbnail, engine,
                   phash, status, created_at
            FROM search_candidates WHERE id = ? AND user_id = ?
            """,
            (candidate_id, user_id),
        ).fetchone()


def atualizar_status_candidato(candidate_id, user_id, status):
    if status not in STATUS_CANDIDATO:
        raise ValueError("Status de candidato inválido.")
    with conexao() as conn:
        cur = conn.execute(
            "UPDATE search_candidates SET status = ? WHERE id = ? AND user_id = ?",
            (status, candidate_id, user_id),
        )
        return cur.rowcount > 0


def listar_todos_candidatos():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, user_id, url, engine, phash, status
            FROM search_candidates
            """
        ).fetchall()


def contar_sites_vazados_por_usuario():
    with conexao() as conn:
        return {
            linha["user_id"]: linha["total"]
            for linha in conn.execute(
                "SELECT user_id, COUNT(*) AS total FROM leak_reports GROUP BY user_id"
            ).fetchall()
        }


def adicionar_hash_oficial(user_id, url, phash):
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO official_image_hashes (user_id, url, phash)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id, url) DO NOTHING
            RETURNING id
            """,
            (user_id, url, phash),
        )
        registro = cur.fetchone()
        return registro["id"] if registro else None


def listar_hashes_oficiais(user_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT url, phash FROM official_image_hashes
            WHERE user_id = ? ORDER BY id
            """,
            (user_id,),
        ).fetchall()


def deve_buscar_instagram(user_id: int) -> bool:
    """Retorna True se nunca buscou ou se passou mais de 24h desde a ultima busca."""
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT ultima_busca_em FROM instagram_busca_controle
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    if linha is None:
        return True
    ultima = linha["ultima_busca_em"]
    from datetime import datetime, timedelta, timezone

    try:
        data_ultima = datetime.fromisoformat(ultima)
    except ValueError:
        return True
    if data_ultima.tzinfo is None:
        data_ultima = data_ultima.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - data_ultima > timedelta(hours=24)


def registrar_busca_instagram(user_id: int) -> None:
    """Registra (ou atualiza) o timestamp da ultima busca de Instagram do usuario."""
    agora = datetime.now(timezone.utc).isoformat()
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO instagram_busca_controle (user_id, ultima_busca_em)
            VALUES (?, ?)
            ON CONFLICT (user_id) DO UPDATE SET ultima_busca_em = excluded.ultima_busca_em
            """,
            (user_id, agora),
        )


def salvar_postagens_instagram(user_id: int, posts: list[dict]) -> int:
    """Salva shortcode, url, miniatura e phash de cada postagem do Instagram. Retorna quantas foram salvas."""
    from app.core import imagens

    salvos = 0
    with conexao() as conn:
        for post in posts:
            shortcode = post.get("shortcode") or post.get("codigo")
            url = post.get("url") or post.get("link")
            miniatura = post.get("miniatura") or ""
            phash = post.get("phash") or ""
            if not shortcode or not url:
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO instagram_post_hashes (user_id, shortcode, url, miniatura, phash)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, shortcode) DO UPDATE
                    SET url = excluded.url, miniatura = excluded.miniatura, phash = excluded.phash, criado_em = CURRENT_TIMESTAMP
                    """,
                    (user_id, shortcode, url, miniatura, phash),
                )
                salvos += 1
            except Exception:
                continue
    return salvos


def listar_postagens_instagram(user_id: int) -> list[dict]:
    """Lista postagens do Instagram salvas para o usuario."""
    with conexao() as conn:
        linhas = conn.execute(
            """
            SELECT shortcode, url, miniatura, phash, criado_em
            FROM instagram_post_hashes
            WHERE user_id = ?
            ORDER BY criado_em DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def obter_ultima_busca_instagram(user_id: int):
    """Retorna a data/hora da ultima busca de Instagram ou None."""
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT ultima_busca_em FROM instagram_busca_controle
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return linha["ultima_busca_em"] if linha else None


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


def remover_criadoras_sem_conta():
    """Remove registros antigos sem conta e retorna arquivos não compartilhados."""
    with conexao() as conn:
        arquivos = [
            linha["arquivo"] for linha in conn.execute(
                """
                SELECT DISTINCT c.arquivo FROM conteudos c
                JOIN criadoras cr ON cr.id = c.criadora_id
                WHERE cr.user_id IS NULL AND NOT EXISTS (
                    SELECT 1 FROM conteudos outro
                    JOIN criadoras dona ON dona.id = outro.criadora_id
                    WHERE outro.arquivo = c.arquivo AND dona.user_id IS NOT NULL
                )
                """
            ).fetchall()
        ]
        conn.execute(
            """DELETE FROM hashes WHERE conteudo_id IN (
                SELECT c.id FROM conteudos c JOIN criadoras cr ON cr.id = c.criadora_id
                WHERE cr.user_id IS NULL
            )"""
        )
        conn.execute(
            "DELETE FROM conteudos WHERE criadora_id IN (SELECT id FROM criadoras WHERE user_id IS NULL)"
        )
        conn.execute(
            "DELETE FROM sites_oficiais WHERE criadora_id IN (SELECT id FROM criadoras WHERE user_id IS NULL)"
        )
        conn.execute("DELETE FROM criadoras WHERE user_id IS NULL")
        return arquivos


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


def obter_configuracao(nome, padrao=""):
    with conexao() as conn:
        linha = conn.execute(
            "SELECT value FROM app_settings WHERE name = ?", (nome,)
        ).fetchone()
    return linha["value"] if linha else padrao


def salvar_configuracao(nome, valor):
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (name, value) VALUES (?, ?)
            ON CONFLICT (name) DO UPDATE SET value = excluded.value
            """,
            (nome, valor),
        )


def salvar_status_busca_usuario(user_id, nome, valor):
    with conexao() as conn:
        if usando_postgresql():
            usuario = conn.execute(
                "SELECT id FROM users WHERE id = ? FOR KEY SHARE", (user_id,)
            ).fetchone()
        else:
            conn.execute("BEGIN IMMEDIATE")
            usuario = conn.execute(
                "SELECT id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if not usuario:
            return False
        conn.execute(
            """INSERT INTO app_settings (name, value) VALUES (?, ?)
               ON CONFLICT (name) DO UPDATE SET value = excluded.value""",
            (nome, valor),
        )
        return True


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


def listar_sites_oficiais_criadora(criadora_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, criadora_id, nome, url, criado_em
            FROM sites_oficiais
            WHERE criadora_id = ?
            ORDER BY url
            """,
            (criadora_id,),
        ).fetchall()


def obter_criadora(criadora_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT c.id, c.nome, c.email, c.handle, c.user_id,
                   u.username, u.email_verified
            FROM criadoras c
            LEFT JOIN users u ON u.id = c.user_id
            WHERE c.id = ?
            """,
            (criadora_id,),
        ).fetchone()


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


def listar_conteudos(user_id=None):
    consulta = """
        SELECT c.id, c.criadora_id, c.tipo, c.arquivo, c.nome_original,
               c.criado_em, cr.nome AS criadora
        FROM conteudos c
        JOIN criadoras cr ON cr.id = c.criadora_id
    """
    parametros = ()
    if user_id is not None:
        consulta += " WHERE cr.user_id = ?"
        parametros = (user_id,)
    consulta += " ORDER BY c.id DESC"
    with conexao() as conn:
        return conn.execute(consulta, parametros).fetchall()


def listar_conteudos_criadora(criadora_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, criadora_id, tipo, arquivo, nome_original, criado_em
            FROM conteudos
            WHERE criadora_id = ?
            ORDER BY id DESC
            """,
            (criadora_id,),
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


def excluir_criadora(criadora_id):
    """Remove a criadora e seus sub-recursos. Retorna os arquivos a excluir."""
    with conexao() as conn:
        criadora = conn.execute(
            "SELECT id, user_id FROM criadoras WHERE id = ?", (criadora_id,)
        ).fetchone()
        if criadora is None:
            return None
        if criadora["user_id"] is not None:
            raise ValueError("criadora associada a conta")

        arquivos = [
            linha["arquivo"]
            for linha in conn.execute(
                "SELECT arquivo FROM conteudos WHERE criadora_id = ?", (criadora_id,)
            ).fetchall()
        ]
        conteudo_ids = [
            linha["id"]
            for linha in conn.execute(
                "SELECT id FROM conteudos WHERE criadora_id = ?", (criadora_id,)
            ).fetchall()
        ]
        for conteudo_id in conteudo_ids:
            conn.execute("DELETE FROM hashes WHERE conteudo_id = ?", (conteudo_id,))
        conn.execute("DELETE FROM conteudos WHERE criadora_id = ?", (criadora_id,))
        conn.execute("DELETE FROM sites_oficiais WHERE criadora_id = ?", (criadora_id,))
        conn.execute("DELETE FROM criadoras WHERE id = ?", (criadora_id,))
        return arquivos


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