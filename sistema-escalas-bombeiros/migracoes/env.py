"""Ambiente do Alembic.

A URL do banco vem de `api.banco`, não do alembic.ini: assim vale a mesma
variável `DATABASE_URL` usada pela aplicação, e não há duas fontes de verdade
sobre onde o banco está.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from api.banco import URL_BANCO  # noqa: E402
from api.modelos import Base  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", URL_BANCO.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def executar_offline() -> None:
    """Gera o SQL sem conectar — útil para revisar antes de aplicar."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def executar_online() -> None:
    conectavel = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with conectavel.connect() as conexao:
        context.configure(
            connection=conexao,
            target_metadata=target_metadata,
            # compare_type detecta mudança de tipo de coluna; sem isso, trocar
            # um String(20) por String(40) passaria despercebido.
            compare_type=True,
            # SQLite não altera coluna no lugar: precisa recriar a tabela.
            render_as_batch=URL_BANCO.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    executar_offline()
else:
    executar_online()
