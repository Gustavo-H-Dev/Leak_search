"""Configuracao somente da suite de testes.

A aplicacao em execucao normal exige PostgreSQL (DATABASE_URL). Os testes usam
um SQLite temporario por meio da flag APP_TESTES_SQLITE, para rodar sem banco
externo; nenhuma rota de producao liga essa flag.
"""

import os

os.environ["APP_TESTES_SQLITE"] = "1"
os.environ.pop("DATABASE_URL", None)
