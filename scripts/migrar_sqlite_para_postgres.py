import os
import shutil
import sqlite3
import sys
from pathlib import Path

from app import db


TABELAS = {
    "criadoras": ("id", "nome", "criado_em"),
    "conteudos": (
        "id",
        "criadora_id",
        "tipo",
        "arquivo",
        "nome_original",
        "criado_em",
    ),
    "hashes": ("id", "conteudo_id", "posicao", "phash"),
    "sites_oficiais": (
        "id",
        "criadora_id",
        "nome",
        "url",
        "criado_em",
    ),
    "users": (
        "id",
        "username",
        "email",
        "password_hash",
        "email_verified",
        "role",
        "public_name",
        "public_handle",
        "created_at",
    ),
    "user_search_photos": (
        "id",
        "user_id",
        "arquivo",
        "nome_original",
        "created_at",
    ),
    "user_official_sites": (
        "id",
        "user_id",
        "created_by",
        "nome",
        "url",
        "created_at",
    ),
    "leak_reports": (
        "id",
        "user_id",
        "reported_by",
        "url",
        "title",
        "source",
        "snippet",
        "thumbnail",
        "engine",
        "metadata_json",
        "created_at",
    ),
    "auth_tokens": (
        "token_hash",
        "user_id",
        "purpose",
        "expires_at",
        "created_at",
    ),
    "auth_sessions": (
        "token_hash",
        "user_id",
        "expires_at",
        "created_at",
    ),
    "app_settings": ("name", "value"),
}
TABELAS_COM_ID = (
    "criadoras",
    "conteudos",
    "hashes",
    "sites_oficiais",
    "users",
    "user_search_photos",
    "user_official_sites",
    "leak_reports",
)


def _tabelas_origem(conn):
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _validar_destino_vazio():
    with db.conexao() as conn:
        for tabela in TABELAS:
            total = conn.execute(f"SELECT COUNT(*) AS total FROM {tabela}").fetchone()[
                "total"
            ]
            if total:
                raise RuntimeError(
                    f"O destino PostgreSQL já contém registros em {tabela}; "
                    "a migração foi cancelada para evitar misturar dados."
                )


def _copiar_banco(caminho_sqlite: Path):
    with sqlite3.connect(caminho_sqlite) as origem:
        origem.row_factory = sqlite3.Row
        tabelas_origem = _tabelas_origem(origem)
        with db.conexao() as destino:
            for tabela, colunas in TABELAS.items():
                if tabela not in tabelas_origem:
                    continue
                colunas_origem = {
                    coluna["name"]
                    for coluna in origem.execute(f"PRAGMA table_info({tabela})")
                }
                if tabela == "sites_oficiais" and "dominio" in colunas_origem:
                    linhas = origem.execute(
                        f"SELECT id, nome, dominio, criado_em FROM {tabela}"
                    )
                    colunas_copia = ("id", "criadora_id", "nome", "url", "criado_em")
                    for linha in linhas:
                        valores = (
                            linha["id"],
                            None,
                            linha["nome"],
                            f"https://{linha['dominio']}/",
                            linha["criado_em"],
                        )
                        placeholders = ", ".join("?" for _ in colunas_copia)
                        nomes = ", ".join(colunas_copia)
                        destino.execute(
                            f"INSERT INTO {tabela} ({nomes}) VALUES ({placeholders}) "
                            "ON CONFLICT DO NOTHING",
                            valores,
                        )
                    continue

                colunas_copia = tuple(
                    coluna for coluna in colunas if coluna in colunas_origem
                )
                if not colunas_copia:
                    continue
                nomes = ", ".join(colunas_copia)
                placeholders = ", ".join("?" for _ in colunas_copia)
                for linha in origem.execute(f"SELECT {nomes} FROM {tabela}"):
                    destino.execute(
                        f"INSERT INTO {tabela} ({nomes}) VALUES ({placeholders}) "
                        "ON CONFLICT DO NOTHING",
                        tuple(linha[coluna] for coluna in colunas_copia),
                    )


def _ajustar_sequencias():
    with db.conexao() as conn:
        for tabela in TABELAS_COM_ID:
            conn.execute(
                f"""
                SELECT setval(
                    pg_get_serial_sequence('{tabela}', 'id'),
                    COALESCE(MAX(id), 1),
                    MAX(id) IS NOT NULL
                )
                FROM {tabela}
                """
            )


def _copiar_imagens():
    origem = Path(os.getenv("MEDIA_SOURCE_DIR", "media/oficiais"))
    destino_configurado = os.getenv("MEDIA_TARGET_DIR", "").strip()
    if not destino_configurado:
        return False
    destino = Path(destino_configurado)
    if not origem.is_dir():
        raise FileNotFoundError(f"Pasta de imagens não encontrada: {origem}")
    destino.mkdir(parents=True, exist_ok=True)
    shutil.copytree(origem, destino, dirs_exist_ok=True, copy_function=shutil.copy2)
    return True


def _copiar_fotos_busca():
    origem = Path(os.getenv("USER_SEARCH_SOURCE_DIR", "media/fotos_busca"))
    destino_configurado = os.getenv("USER_SEARCH_TARGET_DIR", "").strip()
    if not destino_configurado:
        return False
    destino = Path(destino_configurado)
    if not origem.is_dir():
        raise FileNotFoundError(f"Pasta de fotos de busca não encontrada: {origem}")
    destino.mkdir(parents=True, exist_ok=True)
    shutil.copytree(origem, destino, dirs_exist_ok=True, copy_function=shutil.copy2)
    return True


def main():
    caminho_sqlite = Path(os.getenv("SQLITE_PATH", "data/catalogo.db"))
    if not caminho_sqlite.is_file():
        raise FileNotFoundError(f"Banco SQLite não encontrado: {caminho_sqlite}")
    if not db.usando_postgresql():
        raise RuntimeError("Defina DATABASE_URL com a URL do banco PostgreSQL.")

    db.iniciar_banco()
    _validar_destino_vazio()
    _copiar_banco(caminho_sqlite)
    _ajustar_sequencias()
    imagens_copiadas = _copiar_imagens()
    fotos_busca_copiadas = _copiar_fotos_busca()
    print("Migração do banco SQLite para PostgreSQL concluída.")
    if imagens_copiadas:
        print("Arquivos de imagem copiados para MEDIA_TARGET_DIR.")
    else:
        print(
            "ATENÇÃO: imagens não copiadas. Copie media/oficiais para o disco "
            "persistente do Render e configure MEDIA_DIR."
        )
    if fotos_busca_copiadas:
        print("Fotos de busca copiadas para USER_SEARCH_TARGET_DIR.")
    else:
        print(
            "ATENÇÃO: fotos de busca não copiadas. Configure "
            "USER_SEARCH_SOURCE_DIR e USER_SEARCH_TARGET_DIR se houver fotos."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as erro:
        print(f"Erro na migração: {erro}", file=sys.stderr)
        raise
