"""Conexão com o banco.

PostgreSQL em produção (dados fortemente relacionais, bom suporte nativo a
tipos de data e intervalo). SQLite é aceito apenas para testes — nele os
triggers de auditoria de infra/init.sql não existem.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .modelos import Auditoria, Base

#: Quem está alterando e por quê. No Postgres isso vai para variável de sessão
#: e é lido pelos triggers; no SQLite alimenta o registro feito pelo ORM.
_USUARIO: ContextVar[int | None] = ContextVar("usuario_auditoria", default=None)
_MOTIVO: ContextVar[str] = ContextVar("motivo_auditoria", default="")

URL_BANCO = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://escalas:escalas@localhost:5432/escalas"
)

# Render, Railway e Heroku entregam a URL como `postgres://` ou `postgresql://`.
# O SQLAlchemy precisa do driver explícito para usar psycopg3.
if URL_BANCO.startswith("postgres://"):
    URL_BANCO = URL_BANCO.replace("postgres://", "postgresql+psycopg://", 1)
elif URL_BANCO.startswith("postgresql://"):
    URL_BANCO = URL_BANCO.replace("postgresql://", "postgresql+psycopg://", 1)

_kwargs: dict = {"pool_pre_ping": True, "future": True}
if URL_BANCO.startswith("sqlite"):
    _kwargs = {"connect_args": {"check_same_thread": False}, "future": True}

engine = create_engine(URL_BANCO, **_kwargs)
Sessao = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if URL_BANCO.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _ativar_fk(conexao, _registro):  # pragma: no cover - infraestrutura
        cursor = conexao.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def criar_tabelas() -> None:
    """Deixa o esquema em dia, preservando os dados.

    Usa Alembic quando disponível. `create_all` só cria tabelas que faltam —
    nunca adiciona coluna a tabela existente —, e era por isso que cada
    atualização com mudança de modelo exigia apagar o banco.

    Banco que já existia antes do Alembic é "carimbado" na versão atual em vez
    de migrado do zero: as tabelas já estão lá, só falta o registro de versão.
    """
    from sqlalchemy import inspect

    raiz = Path(__file__).resolve().parent.parent
    config_ini = raiz / "alembic.ini"

    if not config_ini.exists():
        Base.metadata.create_all(engine)
        return

    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:  # pragma: no cover - Alembic é opcional no desktop
        Base.metadata.create_all(engine)
        return

    config = Config(str(config_ini))
    config.set_main_option("script_location", str(raiz / "migracoes"))

    inspetor = inspect(engine)
    tabelas = set(inspetor.get_table_names())

    try:
        if "alembic_version" in tabelas:
            command.upgrade(config, "head")
        elif tabelas:
            # Banco anterior ao Alembic: adota sem recriar nada.
            command.stamp(config, "head")
            command.upgrade(config, "head")
        else:
            command.upgrade(config, "head")
    except Exception as erro:  # noqa: BLE001 - migração não pode derrubar o app
        print(f"[banco] migração falhou ({erro}); caindo em create_all.")
        Base.metadata.create_all(engine)


def obter_sessao() -> Iterator[Session]:
    sessao = Sessao()
    try:
        yield sessao
    finally:
        sessao.close()


def definir_contexto_auditoria(
    db: Session, usuario_id: int | None, motivo: str
) -> None:
    """Informa quem alterou e por quê.

    O banco não tem como saber o motivo — ele vem da aplicação. No Postgres vai
    por variável de sessão e é lido pelos triggers (infra/init.sql); no SQLite
    fica em contexto e é usado pelo registro feito no ORM.
    """
    _USUARIO.set(usuario_id)
    _MOTIVO.set(motivo or "")
    if not URL_BANCO.startswith("postgresql"):
        return
    db.execute(
        text("SELECT set_config('app.usuario_id', :u, true)"),
        {"u": str(usuario_id or "")},
    )
    db.execute(
        text("SELECT set_config('app.motivo', :m, true)"), {"m": motivo or ""}
    )


# --------------------------------------------------------------------------- #
# Auditoria no SQLite (aplicativo desktop)
# --------------------------------------------------------------------------- #

#: Tabelas auditadas — as mesmas de infra/init.sql.
AUDITADAS = {
    "plantoes", "escalas", "trocas", "indisponibilidades",
    "preferencias", "feriados", "usuarios", "parametros", "excecoes",
}


def _simples(valor):
    """Converte para um tipo que o JSON aceita."""
    if valor is None or isinstance(valor, (int, float, bool, str, dict, list)):
        return valor
    return str(valor)


def _instantaneo(obj) -> dict:
    """Estado do objeto em tipos que o JSON aceita."""
    return {
        coluna.key: _simples(getattr(obj, coluna.key, None))
        for coluna in inspect(obj).mapper.column_attrs
    }


def _identificador(obj) -> str:
    for campo in ("id", "chave"):
        valor = getattr(obj, campo, None)
        if valor is not None:
            return str(valor)
    return ""


def _registrar(sessao: Session, obj, operacao: str, antes=None, depois=None) -> None:
    sessao.add(
        Auditoria(
            entidade=obj.__tablename__,
            registro_id=_identificador(obj),
            operacao=operacao,
            usuario_id=_USUARIO.get(),
            antes=antes,
            depois=depois,
            motivo=_MOTIVO.get(),
        )
    )


def _coletar(sessao: Session, _contexto, _instancias) -> None:
    """Captura as mudanças ANTES do flush, enquanto o histórico existe.

    Depois do flush o SQLAlchemy limpa o histórico de atributos, então o
    antes/depois precisa ser lido aqui. Já a chave primária de um registro
    novo só é atribuída durante o flush — por isso a gravação fica em
    `_gravar`, logo depois.
    """
    pendentes = []

    for obj in sessao.deleted:
        if getattr(obj, "__tablename__", "") in AUDITADAS:
            pendentes.append((obj, "DELETE", _instantaneo(obj), None))

    for obj in sessao.dirty:
        if getattr(obj, "__tablename__", "") not in AUDITADAS:
            continue
        if not sessao.is_modified(obj):
            continue
        estado = inspect(obj)
        antes, depois = {}, {}
        for coluna in estado.mapper.column_attrs:
            historico = estado.attrs[coluna.key].history
            if not historico.has_changes():
                continue
            antigo_valor = historico.deleted[0] if historico.deleted else None
            novo_valor = historico.added[0] if historico.added else None
            antes[coluna.key] = _simples(antigo_valor)
            depois[coluna.key] = _simples(novo_valor)
        if depois:
            pendentes.append((obj, "UPDATE", antes, depois))

    for obj in sessao.new:
        if getattr(obj, "__tablename__", "") in AUDITADAS:
            pendentes.append((obj, "INSERT", None, None))

    if pendentes:
        sessao.info.setdefault("auditoria_pendente", []).extend(pendentes)


def _gravar(sessao: Session, _contexto) -> None:
    """Grava os registros depois do flush, quando as chaves já existem.

    Usa insert do Core em vez de `sessao.add`: adicionar objetos ao ORM dentro
    do ciclo de flush dispararia outro flush, em recursão.
    """
    pendentes = sessao.info.pop("auditoria_pendente", None)
    if not pendentes:
        return

    linhas = []
    for obj, operacao, antes, depois in pendentes:
        if operacao == "INSERT":
            depois = _instantaneo(obj)
        linhas.append(
            {
                "entidade": obj.__tablename__,
                "registro_id": _identificador(obj),
                "operacao": operacao,
                "usuario_id": _USUARIO.get(),
                "quando": datetime.now(UTC).replace(tzinfo=None),
                "antes": antes,
                "depois": depois,
                "motivo": _MOTIVO.get(),
            }
        )
    if linhas:
        sessao.execute(Auditoria.__table__.insert(), linhas)


if not URL_BANCO.startswith("postgresql"):
    event.listen(Sessao, "before_flush", _coletar)
    event.listen(Sessao, "after_flush", _gravar)
