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
        _migrar_leak_reports()
        _migrar_candidatos_revisao()
        _migrar_plano_usuario()
        _semear_planos()
        purgar_auditoria()
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
                plano TEXT NOT NULL DEFAULT 'gratis',
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
                situacao TEXT NOT NULL DEFAULT 'no_ar',
                situacao_em TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, url)
            );
            CREATE TABLE IF NOT EXISTS contato_abuso_dominio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dominio TEXT NOT NULL UNIQUE,
                url_formulario TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                observacao TEXT NOT NULL DEFAULT '',
                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pedido_remocao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                dominio TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'a_enviar',
                modelo TEXT NOT NULL DEFAULT 'pt',
                texto TEXT NOT NULL DEFAULT '',
                contato_email TEXT NOT NULL DEFAULT '',
                contato_url TEXT NOT NULL DEFAULT '',
                provedor TEXT NOT NULL DEFAULT 'serpapi',
                notas TEXT NOT NULL DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                enviado_em TEXT,
                removido_em TEXT,
                checagem_em TEXT,
                checagem_resultado TEXT NOT NULL DEFAULT '',
                checagem_json TEXT NOT NULL DEFAULT '{}',
                proxima_checagem_em TEXT,
                sugestao_removido INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS pedido_remocao_criadora
            ON pedido_remocao (user_id, status);
            CREATE TABLE IF NOT EXISTS pedido_remocao_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL REFERENCES pedido_remocao(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                titulo TEXT NOT NULL DEFAULT '',
                UNIQUE (pedido_id, url)
            );
            CREATE TABLE IF NOT EXISTS pedido_remocao_evento (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL REFERENCES pedido_remocao(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                nota TEXT NOT NULL DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS preferencia_aviso (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                email_scan INTEGER NOT NULL DEFAULT 1,
                resumo_diario INTEGER NOT NULL DEFAULT 0,
                resumo_semanal INTEGER NOT NULL DEFAULT 0,
                ultimo_resumo_diario TEXT,
                ultimo_resumo_semanal TEXT,
                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS aviso_email (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL,
                chave TEXT NOT NULL UNIQUE,
                assunto TEXT NOT NULL,
                corpo_texto TEXT NOT NULL DEFAULT '',
                corpo_html TEXT NOT NULL DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                enviado_em TEXT,
                erro TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS aviso_email_pendente
            ON aviso_email (user_id, enviado_em);
            CREATE TABLE IF NOT EXISTS auditoria_evento (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                actor_label TEXT NOT NULL DEFAULT '',
                acao TEXT NOT NULL,
                alvo_tipo TEXT NOT NULL DEFAULT '',
                alvo_id TEXT NOT NULL DEFAULT '',
                detalhe TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS auditoria_evento_data
            ON auditoria_evento (criado_em);
            CREATE TABLE IF NOT EXISTS login_tentativa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS login_tentativa_data
            ON login_tentativa (criado_em);
            CREATE TABLE IF NOT EXISTS plano_limite (
                plano TEXT PRIMARY KEY,
                nome TEXT NOT NULL,
                max_perfis INTEGER NOT NULL DEFAULT 5,
                buscas_dia INTEGER NOT NULL DEFAULT 4,
                frequencia_horas INTEGER NOT NULL DEFAULT 24,
                preco_centavos INTEGER NOT NULL DEFAULT 0,
                descricao TEXT NOT NULL DEFAULT '',
                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS modelo_texto (
                codigo TEXT PRIMARY KEY,
                titulo TEXT NOT NULL DEFAULT '',
                conteudo TEXT NOT NULL,
                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    _migrar_leak_reports()
    _migrar_candidatos_revisao()
    _migrar_plano_usuario()
    _semear_planos()
    _semear_dominios_iniciais()
    purgar_auditoria()


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
            plano TEXT NOT NULL DEFAULT 'gratis',
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
            situacao TEXT NOT NULL DEFAULT 'no_ar',
            situacao_em TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, url)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contato_abuso_dominio (
            id BIGSERIAL PRIMARY KEY,
            dominio TEXT NOT NULL UNIQUE,
            url_formulario TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            observacao TEXT NOT NULL DEFAULT '',
            atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pedido_remocao (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            dominio TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'a_enviar',
            modelo TEXT NOT NULL DEFAULT 'pt',
            texto TEXT NOT NULL DEFAULT '',
            contato_email TEXT NOT NULL DEFAULT '',
            contato_url TEXT NOT NULL DEFAULT '',
            provedor TEXT NOT NULL DEFAULT 'serpapi',
            notas TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            enviado_em TEXT,
            removido_em TEXT,
            checagem_em TEXT,
            checagem_resultado TEXT NOT NULL DEFAULT '',
            checagem_json TEXT NOT NULL DEFAULT '{}',
            proxima_checagem_em TEXT,
            sugestao_removido INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS pedido_remocao_criadora ON pedido_remocao (user_id, status)",
        """
        CREATE TABLE IF NOT EXISTS pedido_remocao_item (
            id BIGSERIAL PRIMARY KEY,
            pedido_id BIGINT NOT NULL REFERENCES pedido_remocao(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            titulo TEXT NOT NULL DEFAULT '',
            UNIQUE (pedido_id, url)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pedido_remocao_evento (
            id BIGSERIAL PRIMARY KEY,
            pedido_id BIGINT NOT NULL REFERENCES pedido_remocao(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            nota TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS preferencia_aviso (
            user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            email_scan INTEGER NOT NULL DEFAULT 1,
            resumo_diario INTEGER NOT NULL DEFAULT 0,
            resumo_semanal INTEGER NOT NULL DEFAULT 0,
            ultimo_resumo_diario TEXT,
            ultimo_resumo_semanal TEXT,
            atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS aviso_email (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tipo TEXT NOT NULL,
            chave TEXT NOT NULL UNIQUE,
            assunto TEXT NOT NULL,
            corpo_texto TEXT NOT NULL DEFAULT '',
            corpo_html TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            enviado_em TEXT,
            erro TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX IF NOT EXISTS aviso_email_pendente ON aviso_email (user_id, enviado_em)",
        """
        CREATE TABLE IF NOT EXISTS auditoria_evento (
            id BIGSERIAL PRIMARY KEY,
            actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
            actor_label TEXT NOT NULL DEFAULT '',
            acao TEXT NOT NULL,
            alvo_tipo TEXT NOT NULL DEFAULT '',
            alvo_id TEXT NOT NULL DEFAULT '',
            detalhe TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE INDEX IF NOT EXISTS auditoria_evento_data ON auditoria_evento (criado_em)",
        """
        CREATE TABLE IF NOT EXISTS login_tentativa (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE INDEX IF NOT EXISTS login_tentativa_data ON login_tentativa (criado_em)",
        """
        CREATE TABLE IF NOT EXISTS plano_limite (
            plano TEXT PRIMARY KEY,
            nome TEXT NOT NULL,
            max_perfis INTEGER NOT NULL DEFAULT 5,
            buscas_dia INTEGER NOT NULL DEFAULT 4,
            frequencia_horas INTEGER NOT NULL DEFAULT 24,
            preco_centavos INTEGER NOT NULL DEFAULT 0,
            descricao TEXT NOT NULL DEFAULT '',
            atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS modelo_texto (
            codigo TEXT PRIMARY KEY,
            titulo TEXT NOT NULL DEFAULT '',
            conteudo TEXT NOT NULL,
            atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def _migrar_leak_reports():
    """Situacao do pedido de remocao, sem apagar nenhum relato existente."""
    with conexao() as conn:
        if usando_postgresql():
            for coluna, tipo in (
                ("situacao", "TEXT NOT NULL DEFAULT 'no_ar'"),
                ("situacao_em", "TEXT"),
            ):
                conn.execute(
                    f"ALTER TABLE leak_reports ADD COLUMN IF NOT EXISTS {coluna} {tipo}"
                )
        else:
            existentes = {
                coluna["name"]
                for coluna in conn.execute("PRAGMA table_info(leak_reports)")
            }
            for coluna, tipo in (
                ("situacao", "TEXT NOT NULL DEFAULT 'no_ar'"),
                ("situacao_em", "TEXT"),
            ):
                if coluna not in existentes:
                    conn.execute(
                        f"ALTER TABLE leak_reports ADD COLUMN {coluna} {tipo}"
                    )


def _migrar_candidatos_revisao():
    """Data da revisao do candidato, para a linha do tempo."""
    with conexao() as conn:
        if usando_postgresql():
            conn.execute(
                "ALTER TABLE search_candidates ADD COLUMN IF NOT EXISTS reviewed_at TEXT"
            )
        else:
            existentes = {
                coluna["name"]
                for coluna in conn.execute("PRAGMA table_info(search_candidates)")
            }
            if "reviewed_at" not in existentes:
                conn.execute("ALTER TABLE search_candidates ADD COLUMN reviewed_at TEXT")


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


def ajustar_feedback_dominio(dominio, confirmados=0, falsos=0):
    """Soma (ou subtrai) o feedback do dominio, sem deixar contador negativo."""
    dominio = (dominio or "").strip().lower()
    if not dominio or (not confirmados and not falsos):
        return False
    maior = "GREATEST" if usando_postgresql() else "MAX"
    with conexao() as conn:
        cur = conn.execute(
            f"""
            UPDATE dominio_suspeito
               SET n_confirmados = {maior}(0, n_confirmados + ?),
                   n_falsos = {maior}(0, n_falsos + ?)
             WHERE dominio = ?
            """,
            (int(confirmados), int(falsos), dominio),
        )
        return cur.rowcount > 0


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
            SELECT id, username, email, email_verified, role, plano, public_name,
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
        _anonimizar_auditoria_usuario(conn, user_id)
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


def limite_perfis_monitorados(user_id=None) -> int:
    """Quantos perfis uma conta pode monitorar.

    Precedencia: variavel de ambiente, depois o plano da conta, depois
    app_settings (editavel sem deploy), depois o padrao do codigo.
    """
    do_ambiente = os.getenv("PERFIS_MONITORADOS_LIMITE", "").strip()
    if do_ambiente:
        try:
            return max(1, int(do_ambiente))
        except ValueError:
            pass
    if user_id:
        try:
            return _limites_do_usuario(user_id)["max_perfis"]
        except Exception:
            logger.warning("Plano do usuario %s indisponivel; usando o limite padrao", user_id)
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


STATUS_CANDIDATO = {"pending", "leaked", "not_me", "official", "unsure"}
SITUACOES_REMOCAO = {"no_ar", "em_remocao", "removido"}


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


def limite_buscas_por_dia(user_id=None) -> int:
    """Buscas diarias por criadora: ambiente, plano, app_settings, depois 4."""
    do_ambiente = os.getenv("BUSCAS_POR_DIA_LIMITE", "").strip()
    if do_ambiente:
        try:
            return max(1, int(do_ambiente))
        except ValueError:
            pass
    if user_id:
        try:
            return _limites_do_usuario(user_id)["buscas_dia"]
        except Exception:
            logger.warning("Plano do usuario %s indisponivel; usando o limite padrao", user_id)
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


def _filtros_revisao(
    user_id,
    *,
    status="",
    nota_minima=None,
    nota_maxima=None,
    engine="",
    dominio="",
    desde_iso="",
):
    condicoes = ["user_id = ?"]
    parametros = [user_id]
    if status:
        condicoes.append("status = ?")
        parametros.append(status)
    if nota_minima is not None:
        condicoes.append("COALESCE(nota, 0) >= ?")
        parametros.append(int(nota_minima))
    if nota_maxima is not None:
        condicoes.append("COALESCE(nota, 0) <= ?")
        parametros.append(int(nota_maxima))
    if engine:
        condicoes.append("engine = ?")
        parametros.append(engine)
    if dominio:
        condicoes.append("dominio = ?")
        parametros.append(dominio)
    if desde_iso:
        condicoes.append("COALESCE(ultima_vez_em, created_at) >= ?")
        parametros.append(desde_iso)
    return " AND ".join(condicoes), parametros


def listar_candidatos_revisao_filtrada(
    user_id,
    *,
    status="pending",
    nota_minima=None,
    nota_maxima=None,
    engine="",
    dominio="",
    desde_iso="",
    ordem="nota",
    limite=2000,
):
    """Fila de revisao com os filtros da tela (sempre por criadora).

    A paginacao acontece depois do agrupamento por `chave_grupo`, entao aqui
    vem a lista inteira (limitada) em vez de uma pagina de linhas.
    """
    where, parametros = _filtros_revisao(
        user_id,
        status=status,
        nota_minima=nota_minima,
        nota_maxima=nota_maxima,
        engine=engine,
        dominio=dominio,
        desde_iso=desde_iso,
    )
    if ordem == "recentes":
        ordenacao = (
            " ORDER BY COALESCE(ultima_vez_em, created_at) DESC, nota DESC, id DESC"
        )
    else:
        ordenacao = (
            " ORDER BY nota DESC, novo_para_criadora DESC,"
            " COALESCE(ultima_vez_em, created_at) DESC, id DESC"
        )
    consulta = f"""
        SELECT id, user_id, url, title, source, snippet, thumbnail, engine,
               phash, status, created_at, execucao_id, url_normalizada, dominio,
               nota, motivos_json, classificacao, primeira_vez_em, ultima_vez_em,
               novo_para_criadora, chave_grupo, consulta_log_id, posicao_busca,
               (SELECT modelo FROM consulta_log
                 WHERE id = search_candidates.consulta_log_id) AS consulta_modelo,
               (SELECT consulta_exata FROM consulta_log
                 WHERE id = search_candidates.consulta_log_id) AS consulta_texto
        FROM search_candidates
        WHERE {where}
        {ordenacao}
        LIMIT ?
    """
    with conexao() as conn:
        return conn.execute(
            consulta, tuple(parametros) + (int(limite),)
        ).fetchall()


def contar_candidatos_revisao_filtrada(
    user_id,
    *,
    status="pending",
    nota_minima=None,
    nota_maxima=None,
    engine="",
    dominio="",
    desde_iso="",
):
    where, parametros = _filtros_revisao(
        user_id,
        status=status,
        nota_minima=nota_minima,
        nota_maxima=nota_maxima,
        engine=engine,
        dominio=dominio,
        desde_iso=desde_iso,
    )
    with conexao() as conn:
        linha = conn.execute(
            f"SELECT COUNT(*) AS total FROM search_candidates WHERE {where}",
            tuple(parametros),
        ).fetchone()
    return int((linha["total"] if linha else 0) or 0)


def dominios_com_candidatos(user_id, status="pending"):
    """Dominios da fila para o filtro da tela, do maior para o menor."""
    with conexao() as conn:
        return [
            dict(linha)
            for linha in conn.execute(
                """
                SELECT dominio, COUNT(*) AS total
                FROM search_candidates
                WHERE user_id = ? AND status = ? AND dominio <> ''
                GROUP BY dominio
                ORDER BY total DESC, dominio
                LIMIT 60
                """,
                (user_id, status),
            ).fetchall()
        ]


def contar_painel_revisao(user_id, limiar):
    """Contadores do painel: fila, nao sei, e a situacao dos vazados."""
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status = 'pending' AND COALESCE(nota, 0) >= ?
                       THEN 1 ELSE 0 END) AS novos,
              SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pendentes,
              SUM(CASE WHEN status = 'unsure' THEN 1 ELSE 0 END) AS nao_sei,
              SUM(CASE WHEN status = 'not_me' THEN 1 ELSE 0 END) AS nao_e,
              SUM(CASE WHEN status = 'official' THEN 1 ELSE 0 END) AS oficiais
            FROM search_candidates WHERE user_id = ?
            """,
            (int(limiar), user_id),
        ).fetchone()
        situacoes = {
            item["situacao"] or "no_ar": int(item["total"] or 0)
            for item in conn.execute(
                """
                SELECT situacao, COUNT(*) AS total FROM leak_reports
                WHERE user_id = ? GROUP BY situacao
                """,
                (user_id,),
            ).fetchall()
        }
    return {
        "novos": int((linha["novos"] if linha else 0) or 0),
        "pendentes": int((linha["pendentes"] if linha else 0) or 0),
        "nao_sei": int((linha["nao_sei"] if linha else 0) or 0),
        "nao_e": int((linha["nao_e"] if linha else 0) or 0),
        "oficiais": int((linha["oficiais"] if linha else 0) or 0),
        "no_ar": situacoes.get("no_ar", 0),
        "em_remocao": situacoes.get("em_remocao", 0),
        "removidos": situacoes.get("removido", 0),
    }


def situacoes_leaks(user_id):
    """Situacao de remocao por URL, para a fila de "No ar"."""
    with conexao() as conn:
        return {
            linha["url"]: (linha["situacao"] or "no_ar")
            for linha in conn.execute(
                "SELECT url, situacao FROM leak_reports WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        }


def atualizar_situacao_leak(user_id, url, situacao):
    if situacao not in SITUACOES_REMOCAO:
        raise ValueError("Situacao de remocao invalida.")
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE leak_reports SET situacao = ?, situacao_em = ?
            WHERE user_id = ? AND url = ?
            """,
            (situacao, _agora_utc(), user_id, url),
        )
        return cur.rowcount > 0


def site_vazado_existe(user_id, url):
    with conexao() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM leak_reports WHERE user_id = ? AND url = ?",
                (user_id, url),
            ).fetchone()
            is not None
        )


def remover_site_vazado(user_id, url):
    with conexao() as conn:
        cur = conn.execute(
            "DELETE FROM leak_reports WHERE user_id = ? AND url = ?",
            (user_id, url),
        )
        return cur.rowcount > 0


def ignorar_dominio(dominio):
    dominio = (dominio or "").strip().lower()
    if not dominio:
        return False
    with conexao() as conn:
        conn.execute(
            "INSERT INTO dominio_ignorado (dominio) VALUES (?)"
            " ON CONFLICT (dominio) DO NOTHING",
            (dominio,),
        )
    return True


_COLUNAS_CANDIDATO = """
    id, user_id, url, title, source, snippet, thumbnail, engine, phash, status,
    created_at, execucao_id, url_normalizada, dominio, nota, motivos_json,
    classificacao, primeira_vez_em, ultima_vez_em, novo_para_criadora,
    chave_grupo, consulta_log_id, posicao_busca
"""


def contar_candidatos_dominio_status(user_id, dominio, status):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT COUNT(*) AS total FROM search_candidates
            WHERE user_id = ? AND dominio = ? AND status = ?
            """,
            (user_id, dominio, status),
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
                   phash, status, created_at, dominio, nota, motivos_json,
                   chave_grupo, novo_para_criadora
            FROM search_candidates WHERE id = ? AND user_id = ?
            """,
            (candidate_id, user_id),
        ).fetchone()


def atualizar_status_candidato(candidate_id, user_id, status):
    if status not in STATUS_CANDIDATO:
        raise ValueError("Status de candidato inválido.")
    with conexao() as conn:
        cur = conn.execute(
            "UPDATE search_candidates SET status = ?, reviewed_at = ?"
            " WHERE id = ? AND user_id = ?",
            (status, _agora_utc(), candidate_id, user_id),
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

# --- Kit de remocao: contatos de abuso e pedidos da criadora ----------------

STATUS_PEDIDO_REMOCAO = ("a_enviar", "enviado", "aguardando", "removido", "recusado")
SITUACAO_POR_STATUS_PEDIDO = {
    "a_enviar": "em_remocao",
    "enviado": "em_remocao",
    "aguardando": "em_remocao",
    "removido": "removido",
    "recusado": "no_ar",
}


def _normalizar_dominio(dominio) -> str:
    return (dominio or "").strip().lower().removeprefix("www.")


def listar_contatos_abuso():
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, dominio, url_formulario, email, observacao, atualizado_em
            FROM contato_abuso_dominio ORDER BY dominio
            """
        ).fetchall()


def obter_contato_abuso(dominio):
    dominio = _normalizar_dominio(dominio)
    if not dominio:
        return None
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, dominio, url_formulario, email, observacao, atualizado_em
            FROM contato_abuso_dominio WHERE dominio = ?
            """,
            (dominio,),
        ).fetchone()


def salvar_contato_abuso(dominio, url_formulario="", email="", observacao=""):
    """Cadastro manual do admin: nada aqui e preenchido pelo codigo."""
    dominio = _normalizar_dominio(dominio)
    if not dominio or " " in dominio:
        raise ValueError("Informe o domínio do site (sem http e sem caminho).")
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO contato_abuso_dominio
                (dominio, url_formulario, email, observacao, atualizado_em)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (dominio) DO UPDATE SET
                url_formulario = excluded.url_formulario,
                email = excluded.email,
                observacao = excluded.observacao,
                atualizado_em = excluded.atualizado_em
            """,
            (
                dominio,
                (url_formulario or "").strip(),
                (email or "").strip(),
                (observacao or "").strip(),
                _agora_utc(),
            ),
        )
    return dominio


def excluir_contato_abuso(contato_id):
    with conexao() as conn:
        cur = conn.execute(
            "DELETE FROM contato_abuso_dominio WHERE id = ?", (contato_id,)
        )
        return cur.rowcount > 0


def _dominio_da_url(url) -> str:
    """`leak_reports` guarda a URL; o dominio sai dela na hora de agrupar."""
    from urllib.parse import urlsplit

    try:
        host = urlsplit((url or "").strip()).hostname or ""
    except ValueError:
        host = ""
    return _normalizar_dominio(host)


def _leaks_da_criadora(user_id):
    with conexao() as conn:
        linhas = conn.execute(
            """
            SELECT id, user_id, url, title, source, snippet, engine, situacao,
                   situacao_em, created_at
            FROM leak_reports WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
    return [{**dict(linha), "dominio": _dominio_da_url(linha["url"])} for linha in linhas]


def dominios_confirmados(user_id):
    """Dominios com pelo menos um link confirmado como vazamento."""
    contagens = {}
    for item in _leaks_da_criadora(user_id):
        registro = contagens.setdefault(
            item["dominio"], {"dominio": item["dominio"], "total": 0, "removidos": 0}
        )
        registro["total"] += 1
        if item["situacao"] == "removido":
            registro["removidos"] += 1
    return sorted(contagens.values(), key=lambda item: (-item["total"], item["dominio"]))


def listar_leaks_por_dominio(user_id, dominio):
    dominio = _normalizar_dominio(dominio)
    return [
        item
        for item in _leaks_da_criadora(user_id)
        if item["dominio"] == dominio
    ]


def obter_leak(user_id, url):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT id, user_id, url, title, source, snippet, engine,
                   situacao, situacao_em, created_at
            FROM leak_reports WHERE user_id = ? AND url = ?
            """,
            (user_id, url),
        ).fetchone()
    if linha is None:
        return None
    return {**dict(linha), "dominio": _dominio_da_url(linha["url"])}


def _atualizar_situacao_urls(conn, user_id, urls, situacao):
    if situacao not in SITUACOES_REMOCAO:
        raise ValueError("Situacao de remocao invalida.")
    for url in urls:
        conn.execute(
            """
            UPDATE leak_reports SET situacao = ?, situacao_em = ?
            WHERE user_id = ? AND url = ?
            """,
            (situacao, _agora_utc(), user_id, url),
        )


def criar_pedido_remocao(
    user_id,
    dominio,
    modelo,
    texto,
    contato_email,
    contato_url,
    provedor,
    itens,
    notas="",
):
    """Cria um pedido que pode cobrir varios links do mesmo dominio."""
    urls = [item["url"] for item in itens]
    if not urls:
        raise ValueError("Escolha pelo menos um link para o pedido.")
    agora = _agora_utc()
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO pedido_remocao (
                user_id, dominio, status, modelo, texto, contato_email,
                contato_url, provedor, notas, criado_em, atualizado_em
            )
            VALUES (?, ?, 'a_enviar', ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                user_id,
                _normalizar_dominio(dominio),
                modelo,
                texto,
                (contato_email or "").strip(),
                (contato_url or "").strip(),
                (provedor or "serpapi").strip(),
                (notas or "").strip(),
                agora,
                agora,
            ),
        )
        pedido_id = cur.fetchone()["id"]
        for item in itens:
            conn.execute(
                """
                INSERT INTO pedido_remocao_item (pedido_id, url, titulo)
                VALUES (?, ?, ?)
                ON CONFLICT (pedido_id, url) DO NOTHING
                """,
                (pedido_id, item["url"], item.get("titulo", "")),
            )
        conn.execute(
            "INSERT INTO pedido_remocao_evento (pedido_id, status, nota)"
            " VALUES (?, 'criado', ?)",
            (pedido_id, (notas or "").strip()),
        )
        _atualizar_situacao_urls(conn, user_id, urls, "em_remocao")
    return pedido_id


def listar_itens_pedido(pedido_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, pedido_id, url, titulo FROM pedido_remocao_item
            WHERE pedido_id = ? ORDER BY id
            """,
            (pedido_id,),
        ).fetchall()


def listar_eventos_pedido(pedido_id):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, pedido_id, status, nota, criado_em
            FROM pedido_remocao_evento WHERE pedido_id = ?
            ORDER BY criado_em, id
            """,
            (pedido_id,),
        ).fetchall()


def _pedido_com_itens(conn, linha):
    pedido = dict(linha)
    pedido["itens"] = [
        dict(item)
        for item in conn.execute(
            "SELECT id, url, titulo FROM pedido_remocao_item"
            " WHERE pedido_id = ? ORDER BY id",
            (pedido["id"],),
        ).fetchall()
    ]
    return pedido


def listar_pedidos_remocao(user_id, status=""):
    sql = (
        "SELECT id, user_id, dominio, status, modelo, contato_email,"
        " contato_url, provedor, notas, criado_em, atualizado_em, enviado_em,"
        " removido_em, checagem_em, checagem_resultado, proxima_checagem_em,"
        " sugestao_removido FROM pedido_remocao WHERE user_id = ?"
    )
    parametros = [user_id]
    if status:
        sql += " AND status = ?"
        parametros.append(status)
    sql += " ORDER BY criado_em DESC, id DESC"
    with conexao() as conn:
        return [
            _pedido_com_itens(conn, linha)
            for linha in conn.execute(sql, parametros).fetchall()
        ]


def obter_pedido_remocao(user_id, pedido_id):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT id, user_id, dominio, status, modelo, texto, contato_email,
                   contato_url, provedor, notas, criado_em, atualizado_em,
                   enviado_em, removido_em, checagem_em, checagem_resultado,
                   checagem_json, proxima_checagem_em, sugestao_removido
            FROM pedido_remocao WHERE user_id = ? AND id = ?
            """,
            (user_id, pedido_id),
        ).fetchone()
        if linha is None:
            return None
        pedido = _pedido_com_itens(conn, linha)
        pedido["eventos"] = [
            dict(evento)
            for evento in conn.execute(
                "SELECT id, status, nota, criado_em FROM pedido_remocao_evento"
                " WHERE pedido_id = ? ORDER BY criado_em, id",
                (pedido_id,),
            ).fetchall()
        ]
        return pedido


def atualizar_pedido_status(
    user_id, pedido_id, status, nota="", proxima_checagem_em=None
):
    if status not in STATUS_PEDIDO_REMOCAO:
        raise ValueError("Status de pedido inválido.")
    agora = _agora_utc()
    with conexao() as conn:
        linha = conn.execute(
            "SELECT id, enviado_em FROM pedido_remocao WHERE user_id = ? AND id = ?",
            (user_id, pedido_id),
        ).fetchone()
        if linha is None:
            return None
        enviado_em = linha["enviado_em"]
        if status in ("enviado", "aguardando") and not enviado_em:
            enviado_em = agora
        removido_em = agora if status == "removido" else None
        prazo = proxima_checagem_em if status in ("enviado", "aguardando") else None
        # Removido/recusado encerram a sugestao pendente; os demais mantem.
        sugestao = 0 if status in ("removido", "recusado") else None
        conn.execute(
            """
            UPDATE pedido_remocao SET status = ?, atualizado_em = ?,
                enviado_em = ?, removido_em = ?, proxima_checagem_em = ?,
                sugestao_removido = COALESCE(?, sugestao_removido)
            WHERE user_id = ? AND id = ?
            """,
            (
                status,
                agora,
                enviado_em,
                removido_em,
                prazo,
                sugestao,
                user_id,
                pedido_id,
            ),
        )
        conn.execute(
            "INSERT INTO pedido_remocao_evento (pedido_id, status, nota)"
            " VALUES (?, ?, ?)",
            (pedido_id, status, (nota or "").strip()),
        )
        urls = [
            item["url"]
            for item in conn.execute(
                "SELECT url FROM pedido_remocao_item WHERE pedido_id = ?",
                (pedido_id,),
            ).fetchall()
        ]
        _atualizar_situacao_urls(
            conn, user_id, urls, SITUACAO_POR_STATUS_PEDIDO[status]
        )
    return obter_pedido_remocao(user_id, pedido_id)


def salvar_texto_pedido(user_id, pedido_id, texto):
    with conexao() as conn:
        cur = conn.execute(
            "UPDATE pedido_remocao SET texto = ?, atualizado_em = ?"
            " WHERE user_id = ? AND id = ?",
            ((texto or "").strip(), _agora_utc(), user_id, pedido_id),
        )
        return cur.rowcount > 0


PLANOS_PADRAO = {
    "gratis": {
        "nome": "Gratuito",
        "max_perfis": 5,
        "buscas_dia": 4,
        "frequencia_horas": 24,
        "preco_centavos": 0,
        "descricao": "Conta atual. Sem cobrança; os limites valem desde já.",
    },
    "pro": {
        "nome": "Pro (futuro)",
        "max_perfis": 20,
        "buscas_dia": 12,
        "frequencia_horas": 6,
        "preco_centavos": 0,
        "descricao": "Preparado para quando houver cobrança. Ainda não é vendido.",
    },
}

ACAO_AUDITORIA = {
    "login_falha": "Tentativa de login falhou",
    "login_bloqueado": "Login bloqueado por tentativas",
    "conta_excluida": "Conta e dados excluídos",
    "exportacao": "Exportação de dados",
    "verificacao": "Verificação de remoção",
    "dominio_status": "Domínio aprovado/rejeitado",
    "contato_abuso": "Contato de abuso alterado",
    "modelo_texto": "Modelo de texto alterado",
    "plano_alterado": "Limite de plano alterado",
    "perfil_criado": "Perfil monitorado criado",
    "perfil_removido": "Perfil monitorado removido",
    "usuario_excluido": "Usuário excluído pelo admin",
}


def _migrar_plano_usuario():
    """Plano da conta, para os limites por plano."""
    with conexao() as conn:
        if usando_postgresql():
            conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS plano TEXT NOT NULL DEFAULT 'gratis'"
            )
        else:
            existentes = {
                coluna["name"] for coluna in conn.execute("PRAGMA table_info(users)")
            }
            if "plano" not in existentes:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN plano TEXT NOT NULL DEFAULT 'gratis'"
                )


def _semear_planos():
    agora = _agora_utc()
    with conexao() as conn:
        for plano, dados in PLANOS_PADRAO.items():
            conn.execute(
                """
                INSERT INTO plano_limite
                    (plano, nome, max_perfis, buscas_dia, frequencia_horas,
                     preco_centavos, descricao, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (plano) DO NOTHING
                """,
                (
                    plano,
                    dados["nome"],
                    dados["max_perfis"],
                    dados["buscas_dia"],
                    dados["frequencia_horas"],
                    dados["preco_centavos"],
                    dados["descricao"],
                    agora,
                ),
            )


def listar_planos():
    with conexao() as conn:
        return [
            dict(linha)
            for linha in conn.execute(
                "SELECT plano, nome, max_perfis, buscas_dia, frequencia_horas,"
                " preco_centavos, descricao, atualizado_em FROM plano_limite"
                " ORDER BY preco_centavos, plano"
            ).fetchall()
        ]


def obter_plano(plano):
    with conexao() as conn:
        linha = conn.execute(
            "SELECT plano, nome, max_perfis, buscas_dia, frequencia_horas,"
            " preco_centavos, descricao FROM plano_limite WHERE plano = ?",
            ((plano or "gratis").strip().lower(),),
        ).fetchone()
    return dict(linha) if linha else None


def salvar_plano(plano, nome, max_perfis, buscas_dia, frequencia_horas, descricao=""):
    plano = (plano or "").strip().lower()
    if not plano or not re.fullmatch(r"[a-z0-9_-]{2,32}", plano):
        raise ValueError("Código de plano inválido.")
    if not (nome or "").strip():
        raise ValueError("Informe o nome do plano.")
    valores = {
        "max_perfis": max(1, int(max_perfis)),
        "buscas_dia": max(1, int(buscas_dia)),
        "frequencia_horas": max(1, int(frequencia_horas)),
    }
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO plano_limite
                (plano, nome, max_perfis, buscas_dia, frequencia_horas,
                 preco_centavos, descricao, atualizado_em)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT (plano) DO UPDATE SET
                nome = excluded.nome,
                max_perfis = excluded.max_perfis,
                buscas_dia = excluded.buscas_dia,
                frequencia_horas = excluded.frequencia_horas,
                descricao = excluded.descricao,
                atualizado_em = excluded.atualizado_em
            """,
            (
                plano,
                nome.strip()[:80],
                valores["max_perfis"],
                valores["buscas_dia"],
                valores["frequencia_horas"],
                (descricao or "").strip()[:500],
                _agora_utc(),
            ),
        )


def contar_dados_conta(user_id):
    """O que a exclusao vai apagar, para a pessoa ver antes de confirmar."""
    with conexao() as conn:
        def total(tabela, coluna):
            linha = conn.execute(
                f"SELECT COUNT(*) AS total FROM {tabela} WHERE {coluna} = ?",
                (user_id,),
            ).fetchone()
            return int((linha["total"] if linha else 0) or 0)

        return {
            "candidatos": total("search_candidates", "user_id"),
            "perfis": total("perfil_monitorado", "criadora_id"),
            "variantes": total("nome_variante", "criadora_id"),
            "execucoes": total("busca_execucao", "criadora_id"),
            "pedidos": total("pedido_remocao", "user_id"),
            "fotos": total("user_search_photos", "user_id"),
        }


def atualizar_plano_usuario(user_id, plano):
    plano = (plano or "gratis").strip().lower()
    with conexao() as conn:
        if not conn.execute(
            "SELECT plano FROM plano_limite WHERE plano = ?", (plano,)
        ).fetchone():
            raise ValueError("Plano inexistente.")
        cur = conn.execute(
            "UPDATE users SET plano = ? WHERE id = ?", (plano, user_id)
        )
        return cur.rowcount > 0


def plano_do_usuario(user_id):
    with conexao() as conn:
        linha = conn.execute(
            "SELECT u.plano FROM users u WHERE u.id = ?", (user_id,)
        ).fetchone()
    return (linha["plano"] if linha and linha["plano"] else "gratis")


def _limites_do_usuario(user_id):
    plano = obter_plano(plano_do_usuario(user_id)) or obter_plano("gratis") or {}
    return {
        "max_perfis": int(plano.get("max_perfis") or LIMITE_PERFIS_PADRAO),
        "buscas_dia": int(plano.get("buscas_dia") or LIMITE_BUSCAS_DIA_PADRAO),
        "frequencia_horas": int(plano.get("frequencia_horas") or 24),
    }


# --- Auditoria ---------------------------------------------------------------


def registrar_auditoria(
    acao, actor=None, alvo_tipo="", alvo_id="", detalhe="", ip=""
):
    """Trilha de acoes sensiveis. Nunca recebe senha, token ou trecho de site."""
    dados_actor = dict(actor) if actor else {}
    actor_user_id = dados_actor.get("id")
    actor_label = ""
    if dados_actor:
        actor_label = (
            dados_actor.get("username") or dados_actor.get("public_name") or ""
        )[:80]
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO auditoria_evento
                (actor_user_id, actor_label, acao, alvo_tipo, alvo_id,
                 detalhe, ip, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                actor_label,
                (acao or "")[:60],
                (alvo_tipo or "")[:40],
                str(alvo_id or "")[:120],
                (detalhe or "")[:1000],
                (ip or "")[:64],
                _agora_utc(),
            ),
        )


def listar_auditoria(limite=100, acao=""):
    sql = (
        "SELECT id, actor_user_id, actor_label, acao, alvo_tipo, alvo_id,"
        " detalhe, ip, criado_em FROM auditoria_evento"
    )
    parametros = []
    if acao:
        sql += " WHERE acao = ?"
        parametros.append(acao)
    sql += " ORDER BY id DESC LIMIT ?"
    parametros.append(max(1, int(limite)))
    with conexao() as conn:
        return [dict(linha) for linha in conn.execute(sql, parametros).fetchall()]


def dias_retencao_auditoria() -> int:
    try:
        return max(1, int(os.getenv("RETENCAO_AUDITORIA_DIAS", "365")))
    except ValueError:
        return 365


def purgar_auditoria(dias=None):
    """Remove a trilha mais antiga que o prazo de retencao (12 meses por padrao)."""
    prazo = dias or dias_retencao_auditoria()
    corte = (datetime.now(timezone.utc) - timedelta(days=prazo)).isoformat()
    with conexao() as conn:
        cur = conn.execute(
            "DELETE FROM auditoria_evento WHERE criado_em < ?", (corte,)
        )
        conn.execute("DELETE FROM login_tentativa WHERE criado_em < ?", (corte,))
        return cur.rowcount


def _anonimizar_auditoria_usuario(conn, user_id):
    conn.execute(
        """
        UPDATE auditoria_evento
           SET actor_user_id = NULL, actor_label = ''
         WHERE actor_user_id = ?
        """,
        (user_id,),
    )
    conn.execute(
        """
        UPDATE auditoria_evento SET alvo_id = ''
         WHERE alvo_tipo = 'usuario' AND alvo_id = ?
        """,
        (str(user_id),),
    )


# --- Login: limite de tentativas --------------------------------------------

MAX_TENTATIVAS_LOGIN = 5
JANELA_TENTATIVAS_MINUTOS = 15


def registrar_tentativa_login(username, ip=""):
    with conexao() as conn:
        conn.execute(
            "INSERT INTO login_tentativa (username, ip, criado_em) VALUES (?, ?, ?)",
            ((username or "").strip()[:190].lower(), (ip or "")[:64], _agora_utc()),
        )


def limpar_tentativas_login(username, ip=""):
    """Limpa só as falhas deste usuário; não apaga as de outra pessoa no mesmo IP."""
    with conexao() as conn:
        conn.execute(
            "DELETE FROM login_tentativa WHERE lower(username) = lower(?)",
            ((username or "").strip(),),
        )


def tentativas_login_recentes(username, ip="", janela_minutos=JANELA_TENTATIVAS_MINUTOS):
    corte = (
        datetime.now(timezone.utc) - timedelta(minutes=janela_minutos)
    ).isoformat()
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT
                SUM(CASE WHEN lower(username) = lower(?) THEN 1 ELSE 0 END) AS por_usuario,
                SUM(CASE WHEN ip != '' AND ip = ? THEN 1 ELSE 0 END) AS por_ip
            FROM login_tentativa WHERE criado_em >= ?
            """,
            ((username or "").strip(), (ip or "").strip(), corte),
        ).fetchone()
    return {
        "por_usuario": int((linha["por_usuario"] if linha else 0) or 0),
        "por_ip": int((linha["por_ip"] if linha else 0) or 0),
    }


# --- Uso e custo -------------------------------------------------------------


def uso_buscas(dias=7):
    """Buscas por criadora e por dia, falhas por tipo e limite do plano."""
    dias = max(1, min(90, int(dias)))
    hoje = datetime.now(timezone.utc).date()
    inicio = (hoje - timedelta(days=dias - 1)).isoformat()
    datas = [(hoje - timedelta(days=indice)).isoformat() for indice in range(dias - 1, -1, -1)]
    with conexao() as conn:
        linhas = conn.execute(
            """
            SELECT e.criadora_id, u.username, u.public_name, u.plano, e.status,
                   e.motivo_falha, e.criada_em
            FROM busca_execucao e JOIN users u ON u.id = e.criadora_id
            WHERE e.criada_em >= ?
            ORDER BY e.criada_em
            """,
            (inicio,),
        ).fetchall()
    criadoras = {}
    totais_dia = {data: 0 for data in datas}
    falhas_tipo = {}
    for linha in linhas:
        data = str(linha["criada_em"] or "")[:10]
        if data not in totais_dia:
            continue
        chave = linha["criadora_id"]
        registro = criadoras.setdefault(
            chave,
            {
                "criadora_id": chave,
                "nome": linha["public_name"] or linha["username"],
                "plano": linha["plano"] or "gratis",
                "por_dia": {dia: 0 for dia in datas},
                "total": 0,
                "falhas": 0,
                "hoje": 0,
            },
        )
        registro["por_dia"][data] += 1
        registro["total"] += 1
        if data == hoje.isoformat():
            registro["hoje"] += 1
        totais_dia[data] += 1
        if linha["status"] in {"erro", "avisos"}:
            registro["falhas"] += 1
            tipo = (linha["motivo_falha"] or "sem_motivo").strip() or "sem_motivo"
            falhas_tipo[tipo] = falhas_tipo.get(tipo, 0) + 1
    limites = {}
    for chave, registro in criadoras.items():
        limites = _limites_do_usuario(chave)
        registro["limite_dia"] = limites["buscas_dia"]
    return {
        "dias": datas,
        "criadoras": sorted(
            criadoras.values(), key=lambda item: (-item["total"], item["nome"])
        ),
        "total_por_dia": totais_dia,
        "total": len(linhas),
        "falhas_por_tipo": sorted(
            ({"tipo": tipo, "total": total} for tipo, total in falhas_tipo.items()),
            key=lambda item: -item["total"],
        ),
    }


# --- Sinais de abuso (somente alerta) ---------------------------------------


def sinais_abuso():
    """Alertas para o admin; nada aqui bloqueia conta automaticamente."""
    agora = datetime.now(timezone.utc)
    corte_24h = (agora - timedelta(hours=24)).isoformat()
    corte_7d = (agora - timedelta(days=7)).isoformat()
    sinais = []
    with conexao() as conn:
        for linha in conn.execute(
            """
            SELECT lower(username) AS username, COUNT(*) AS total
            FROM login_tentativa WHERE criado_em >= ? AND username != ''
            GROUP BY lower(username) HAVING COUNT(*) >= ?
            ORDER BY total DESC LIMIT 10
            """,
            (corte_24h, MAX_TENTATIVAS_LOGIN),
        ).fetchall():
            sinais.append(
                {
                    "tipo": "login",
                    "titulo": f"{linha['total']} falhas de login para {linha['username']}",
                    "detalhe": "Confira se é a própria pessoa errando a senha ou tentativa de acesso.",
                }
            )
        for linha in conn.execute(
            """
            SELECT e.criadora_id, u.username, u.public_name, COUNT(*) AS total
            FROM busca_execucao e JOIN users u ON u.id = e.criadora_id
            WHERE e.criada_em >= ? AND e.status = 'erro'
            GROUP BY e.criadora_id, u.username, u.public_name
            HAVING COUNT(*) >= 3 ORDER BY total DESC LIMIT 10
            """,
            (corte_24h,),
        ).fetchall():
            sinais.append(
                {
                    "tipo": "falhas_busca",
                    "titulo": f"{linha['total']} buscas falharam em 24 h para "
                    f"{linha['public_name'] or linha['username']}",
                    "detalhe": "Pode ser chave sem saldo, bloqueio do provedor ou configuração.",
                }
            )
        for linha in conn.execute(
            """
            SELECT p.user_id, u.username, u.public_name, COUNT(*) AS total
            FROM pedido_remocao p JOIN users u ON u.id = p.user_id
            WHERE p.checagem_resultado = 'erro' AND p.checagem_em >= ?
            GROUP BY p.user_id, u.username, u.public_name
            HAVING COUNT(*) >= 3 ORDER BY total DESC LIMIT 10
            """,
            (corte_24h,),
        ).fetchall():
            sinais.append(
                {
                    "tipo": "verificacoes",
                    "titulo": f"{linha['total']} verificações falharam em 24 h para "
                    f"{linha['public_name'] or linha['username']}",
                    "detalhe": "Pode ser bloqueio do buscador ou links inválidos; confira antes de agir.",
                }
            )
        for linha in conn.execute(
            """
            SELECT actor_label, COUNT(*) AS total,
                   SUM(CASE WHEN acao = 'perfil_removido' THEN 1 ELSE 0 END) AS remocoes
            FROM auditoria_evento
            WHERE criado_em >= ? AND acao IN ('perfil_criado', 'perfil_removido')
            GROUP BY actor_label
            HAVING COUNT(*) >= 3 AND SUM(CASE WHEN acao = 'perfil_removido' THEN 1 ELSE 0 END) >= 2
            ORDER BY total DESC LIMIT 10
            """,
            (corte_7d,),
        ).fetchall():
            sinais.append(
                {
                    "tipo": "perfis",
                    "titulo": f"Perfis criados e removidos em sequência por {linha['actor_label'] or 'conta'}",
                    "detalhe": f"{linha['total']} eventos, {linha['remocoes']} remoções em 7 dias.",
                }
            )
        for linha in conn.execute(
            """
            SELECT e.criadora_id, u.username, u.public_name, u.plano, COUNT(*) AS total
            FROM busca_execucao e JOIN users u ON u.id = e.criadora_id
            WHERE e.criada_em >= ?
            GROUP BY e.criadora_id, u.username, u.public_name, u.plano
            ORDER BY total DESC LIMIT 20
            """,
            (corte_24h,),
        ).fetchall():
            limite = _limites_do_usuario(linha["criadora_id"])["buscas_dia"]
            if linha["total"] > limite * 2:
                sinais.append(
                    {
                        "tipo": "volume",
                        "titulo": f"Volume anormal: {linha['total']} buscas em 24 h para "
                        f"{linha['public_name'] or linha['username']}",
                        "detalhe": f"O plano {linha['plano']} permite {limite} por dia.",
                    }
                )
        for linha in conn.execute(
            """
            SELECT actor_label, COUNT(*) AS total FROM auditoria_evento
            WHERE criado_em >= ? AND acao = 'conta_excluida'
            GROUP BY actor_label HAVING COUNT(*) >= 3 ORDER BY total DESC LIMIT 5
            """,
            (corte_24h,),
        ).fetchall():
            sinais.append(
                {
                    "tipo": "exclusoes",
                    "titulo": f"{linha['total']} contas excluídas em 24 h",
                    "detalhe": "Confirme se foram pedidos legítimos das titulares.",
                }
            )
    return sinais


# --- Modelos de texto editáveis ---------------------------------------------


def obter_modelo_texto(codigo):
    with conexao() as conn:
        linha = conn.execute(
            "SELECT codigo, titulo, conteudo, atualizado_em FROM modelo_texto"
            " WHERE codigo = ?",
            ((codigo or "").strip(),),
        ).fetchone()
    return dict(linha) if linha else None


def salvar_modelo_texto(codigo, titulo, conteudo):
    codigo = (codigo or "").strip()
    if not re.fullmatch(r"[a-z]{2,8}", codigo):
        raise ValueError("Código de modelo inválido.")
    if not (conteudo or "").strip():
        raise ValueError("O modelo não pode ficar vazio.")
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO modelo_texto (codigo, titulo, conteudo, atualizado_em)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (codigo) DO UPDATE SET
                titulo = excluded.titulo,
                conteudo = excluded.conteudo,
                atualizado_em = excluded.atualizado_em
            """,
            (codigo, (titulo or "").strip()[:80], conteudo[:20000], _agora_utc()),
        )


def restaurar_modelo_texto(codigo):
    with conexao() as conn:
        conn.execute("DELETE FROM modelo_texto WHERE codigo = ?", ((codigo or ""),))


def registrar_checagem_pedido(
    user_id, pedido_id, resultado, detalhes, proxima_checagem_em, resumo
):
    """Guarda o resultado da checagem; nunca muda o status sozinho."""
    with conexao() as conn:
        cur = conn.execute(
            """
            UPDATE pedido_remocao SET checagem_em = ?, checagem_resultado = ?,
                checagem_json = ?, proxima_checagem_em = ?,
                sugestao_removido = ?
            WHERE user_id = ? AND id = ?
            """,
            (
                _agora_utc(),
                resultado,
                json.dumps(detalhes, ensure_ascii=False, default=str),
                proxima_checagem_em,
                1 if resultado == "ausente" else 0,
                user_id,
                pedido_id,
            ),
        )
        if cur.rowcount:
            conn.execute(
                "INSERT INTO pedido_remocao_evento (pedido_id, status, nota)"
                " VALUES (?, 'checagem', ?)",
                (pedido_id, resumo),
            )
        return cur.rowcount > 0


# --- Avisos, linha do tempo e indicadores -----------------------------------


def obter_preferencia_aviso(user_id):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT user_id, email_scan, resumo_diario, resumo_semanal,
                   ultimo_resumo_diario, ultimo_resumo_semanal, atualizado_em
            FROM preferencia_aviso WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    if linha is None:
        return {
            "user_id": user_id,
            "email_scan": 1,
            "resumo_diario": 0,
            "resumo_semanal": 0,
            "ultimo_resumo_diario": None,
            "ultimo_resumo_semanal": None,
        }
    return dict(linha)


def salvar_preferencia_aviso(user_id, email_scan, resumo_diario, resumo_semanal):
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO preferencia_aviso
                (user_id, email_scan, resumo_diario, resumo_semanal, atualizado_em)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                email_scan = excluded.email_scan,
                resumo_diario = excluded.resumo_diario,
                resumo_semanal = excluded.resumo_semanal,
                atualizado_em = excluded.atualizado_em
            """,
            (
                user_id,
                1 if email_scan else 0,
                1 if resumo_diario else 0,
                1 if resumo_semanal else 0,
                _agora_utc(),
            ),
        )


def registrar_resumo_enviado(user_id, tipo, quando):
    coluna = "ultimo_resumo_diario" if tipo == "resumo_diario" else "ultimo_resumo_semanal"
    with conexao() as conn:
        cur = conn.execute(
            f"UPDATE preferencia_aviso SET {coluna} = ? WHERE user_id = ?",
            (quando, user_id),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO preferencia_aviso (user_id) VALUES (?)", (user_id,)
            )
            conn.execute(
                f"UPDATE preferencia_aviso SET {coluna} = ? WHERE user_id = ?",
                (quando, user_id),
            )
        return True


def criar_aviso(user_id, tipo, chave, assunto, corpo_texto, corpo_html):
    """Grava o aviso na caixa de saida; `chave` evita envio duplicado."""
    with conexao() as conn:
        cur = conn.execute(
            """
            INSERT INTO aviso_email
                (user_id, tipo, chave, assunto, corpo_texto, corpo_html)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (chave) DO NOTHING
            RETURNING id
            """,
            (user_id, tipo, chave, assunto, corpo_texto, corpo_html),
        )
        linha = cur.fetchone()
    return linha["id"] if linha else None


def listar_avisos_pendentes(limite=20):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT a.id, a.user_id, a.tipo, a.chave, a.assunto, a.corpo_texto,
                   a.corpo_html, a.criado_em, u.email, u.email_verified
            FROM aviso_email a JOIN users u ON u.id = a.user_id
            WHERE a.enviado_em IS NULL AND a.erro = ''
            ORDER BY a.id
            LIMIT ?
            """,
            (int(limite),),
        ).fetchall()


def marcar_aviso_enviado(aviso_id):
    with conexao() as conn:
        conn.execute(
            "UPDATE aviso_email SET enviado_em = ?, erro = '' WHERE id = ?",
            (_agora_utc(), aviso_id),
        )


def marcar_aviso_erro(aviso_id, erro):
    with conexao() as conn:
        conn.execute(
            "UPDATE aviso_email SET erro = ? WHERE id = ?",
            ((erro or "")[:1000], aviso_id),
        )


def listar_avisos_usuario(user_id, limite=12):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, tipo, assunto, criado_em, enviado_em, erro
            FROM aviso_email WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, int(limite)),
        ).fetchall()


def candidatos_novos_da_execucao(execucao_id, limiar):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT url, dominio, title, nota, created_at
            FROM search_candidates
            WHERE execucao_id = ? AND COALESCE(nota, 0) >= ?
            ORDER BY nota DESC, id
            """,
            (execucao_id, int(limiar)),
        ).fetchall()


def contar_novos_desde(user_id, desde_iso, limiar):
    with conexao() as conn:
        linha = conn.execute(
            """
            SELECT COUNT(*) AS total FROM search_candidates
            WHERE user_id = ? AND COALESCE(nota, 0) >= ?
              AND created_at >= ?
            """,
            (user_id, int(limiar), desde_iso),
        ).fetchone()
    return int((linha["total"] if linha else 0) or 0)


def top_candidatos_desde(user_id, desde_iso, limiar, limite=3):
    with conexao() as conn:
        return conn.execute(
            """
            SELECT dominio, title, nota FROM search_candidates
            WHERE user_id = ? AND COALESCE(nota, 0) >= ?
              AND created_at >= ?
            ORDER BY nota DESC, id
            LIMIT ?
            """,
            (user_id, int(limiar), desde_iso, int(limite)),
        ).fetchall()


def _pedido_por_url(user_id):
    with conexao() as conn:
        linhas = conn.execute(
            """
            SELECT pi.url, p.id, p.status, p.criado_em, p.enviado_em,
                   p.removido_em
            FROM pedido_remocao_item pi
            JOIN pedido_remocao p ON p.id = pi.pedido_id
            WHERE p.user_id = ?
            ORDER BY p.id
            """,
            (user_id,),
        ).fetchall()
    por_url = {}
    for linha in linhas:
        por_url[linha["url"]] = dict(linha)
    return por_url


def dados_linha_do_tempo(user_id):
    """Um registro por link, juntando deteccao, revisao e pedido de remocao."""
    with conexao() as conn:
        candidatos = conn.execute(
            """
            SELECT url, dominio, title, status, created_at, primeira_vez_em,
                   reviewed_at
            FROM search_candidates WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
    linhas = {}
    for candidato in candidatos:
        linhas[candidato["url"]] = {
            "url": candidato["url"],
            "dominio": candidato["dominio"],
            "titulo": candidato["title"],
            "status_revisao": candidato["status"],
            "detectado_em": candidato["primeira_vez_em"] or candidato["created_at"],
            "revisado_em": candidato["reviewed_at"],
            "confirmado_em": None,
            "situacao": "",
            "pedido_id": None,
            "pedido_status": "",
            "enviado_em": None,
            "removido_em": None,
            "pedido_criado_em": None,
        }
    for leak in _leaks_da_criadora(user_id):
        registro = linhas.setdefault(
            leak["url"],
            {
                "url": leak["url"],
                "dominio": leak["dominio"],
                "titulo": leak["title"],
                "status_revisao": "leaked",
                "detectado_em": leak["created_at"],
                "revisado_em": leak["created_at"],
                "confirmado_em": None,
                "situacao": "",
                "pedido_id": None,
                "pedido_status": "",
                "enviado_em": None,
                "removido_em": None,
                "pedido_criado_em": None,
            },
        )
        registro["confirmado_em"] = leak["created_at"]
        registro["situacao"] = leak["situacao"]
    for url, pedido in _pedido_por_url(user_id).items():
        registro = linhas.get(url)
        if registro is None:
            continue
        registro["pedido_id"] = pedido["id"]
        registro["pedido_status"] = pedido["status"]
        registro["pedido_criado_em"] = pedido["criado_em"]
        registro["enviado_em"] = pedido["enviado_em"]
        registro["removido_em"] = pedido["removido_em"]
    return sorted(
        linhas.values(),
        key=lambda item: item["revisado_em"]
        or item["enviado_em"]
        or item["detectado_em"]
        or "",
        reverse=True,
    )


def _dias_entre(inicio, fim):
    try:
        a = datetime.fromisoformat(str(inicio))
        b = datetime.fromisoformat(str(fim))
    except (TypeError, ValueError):
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return max(0.0, (b - a).total_seconds() / 86400)


def indicadores_remocao(user_id):
    linhas = dados_linha_do_tempo(user_id)
    confirmados = [item for item in linhas if item["confirmado_em"]]
    removidos = [
        item
        for item in confirmados
        if item["situacao"] == "removido" or item["pedido_status"] == "removido"
    ]
    contagem = {}
    for item in confirmados:
        contagem[item["dominio"]] = contagem.get(item["dominio"], 0) + 1
    reincidentes = sorted(
        ({"dominio": dominio, "links": total} for dominio, total in contagem.items() if total >= 2),
        key=lambda item: (-item["links"], item["dominio"]),
    )
    tempos = [
        _dias_entre(item["pedido_criado_em"], item["removido_em"])
        for item in removidos
        if item["pedido_criado_em"] and item["removido_em"]
    ]
    tempos = [tempo for tempo in tempos if tempo is not None]
    return {
        "total_links": len(linhas),
        "confirmados": len(confirmados),
        "removidos": len(removidos),
        "taxa_remocao": (len(removidos) / len(confirmados)) if confirmados else 0.0,
        "reincidentes": reincidentes,
        "tempo_medio_dias": (sum(tempos) / len(tempos)) if tempos else None,
        "pedidos": len(listar_pedidos_remocao(user_id)),
    }
