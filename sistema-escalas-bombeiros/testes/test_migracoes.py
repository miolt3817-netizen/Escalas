"""Migrações — Parte 2, evolução do esquema sem perder dados.

`create_all` só cria tabelas que faltam; nunca adiciona coluna a tabela que já
existe. Era por isso que toda atualização com mudança de modelo exigia apagar o
banco. Estes testes garantem que o caminho com Alembic funciona de verdade.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _alembic(banco: Path, *args: str) -> subprocess.CompletedProcess:
    import os

    ambiente = {**os.environ, "DATABASE_URL": f"sqlite:///{banco}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=RAIZ, env=ambiente, capture_output=True, text=True, timeout=300,
    )


def test_existe_migracao_inicial():
    versoes = list((RAIZ / "migracoes" / "versions").glob("*.py"))
    assert versoes, "nenhuma migração registrada"


def test_upgrade_cria_o_esquema_completo(tmp_path):
    import sqlite3

    banco = tmp_path / "novo.db"
    r = _alembic(banco, "upgrade", "head")
    assert r.returncode == 0, r.stderr[-500:]

    conexao = sqlite3.connect(banco)
    tabelas = {
        linha[0] for linha in
        conexao.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conexao.close()

    esperadas = {
        "usuarios", "plantoes", "escalas", "trocas", "indisponibilidades",
        "preferencias", "feriados", "parametros", "auditoria", "explicacoes",
        "solve_snapshots", "excecoes", "jobs", "alembic_version",
    }
    assert esperadas <= tabelas, f"faltando: {esperadas - tabelas}"


def test_modelo_e_migracoes_estao_sincronizados(tmp_path):
    """Autogenerate num banco já migrado não pode encontrar diferença.

    Se encontrar, alguém mudou o modelo sem gerar a migração — e o próximo
    deploy quebraria.
    """
    banco = tmp_path / "sincronia.db"
    assert _alembic(banco, "upgrade", "head").returncode == 0

    r = _alembic(banco, "check")
    if "No such command" in r.stderr:  # Alembic antigo não tem `check`
        pytest.skip("alembic check indisponível nesta versão")
    assert r.returncode == 0, (
        "modelo mudou sem migração correspondente:\n" + r.stdout + r.stderr
    )


def test_colunas_com_padrao_tem_server_default():
    """Sem `server_default`, adicionar coluna a tabela COM DADOS falha.

    O autogenerate emite `ALTER TABLE ... NOT NULL` sem valor, e o banco
    recusa: "Cannot add a NOT NULL column with default value NULL". O erro só
    aparece onde há linhas — nunca no banco vazio de um teste comum.
    """
    import ast

    fonte = (RAIZ / "api" / "modelos.py").read_text()
    arvore = ast.parse(fonte)
    faltando = []

    for classe in [n for n in arvore.body if isinstance(n, ast.ClassDef)]:
        for item in classe.body:
            if not isinstance(item, ast.AnnAssign) or item.value is None:
                continue
            chamada = item.value
            if not (isinstance(chamada, ast.Call)
                    and getattr(chamada.func, "id", "") == "mapped_column"):
                continue

            nomes = {k.arg for k in chamada.keywords}
            if "default" not in nomes or "server_default" in nomes:
                continue
            if nomes & {"primary_key"}:
                continue

            # coluna opcional aceita NULL: o ALTER TABLE funciona sem valor
            anotacao = ast.unparse(item.annotation)
            if "None" in anotacao or "nullable=True" in ast.unparse(chamada):
                continue

            padrao = next(k.value for k in chamada.keywords if k.arg == "default")
            if isinstance(padrao, ast.Attribute):  # default=func.now()
                continue

            faltando.append(f"{classe.name}.{item.target.id}")

    assert not faltando, (
        "colunas com default de Python e sem server_default: " + ", ".join(faltando)
    )


def test_banco_anterior_ao_alembic_e_adotado(tmp_path):
    """Quem já tinha banco criado por `create_all` não pode perder os dados."""
    import os
    import sqlite3

    banco = tmp_path / "legado.db"
    ambiente = {**os.environ, "DATABASE_URL": f"sqlite:///{banco}"}

    # cria o banco do jeito antigo, com um registro dentro
    criar = subprocess.run(
        [sys.executable, "-c",
         "from api.modelos import Base\n"
         "from api.banco import engine\n"
         "Base.metadata.create_all(engine)\n"
         "from api.banco import Sessao\n"
         "from api import modelos as m\n"
         "db = Sessao()\n"
         "db.add(m.Parametro(chave='teste', valor='42'))\n"
         "db.commit()\n"],
        cwd=RAIZ, env=ambiente, capture_output=True, text=True, timeout=180,
    )
    assert criar.returncode == 0, criar.stderr[-400:]

    # agora a aplicação sobe e deve adotar o banco sem apagar nada
    adotar = subprocess.run(
        [sys.executable, "-c", "from api.banco import criar_tabelas; criar_tabelas()"],
        cwd=RAIZ, env=ambiente, capture_output=True, text=True, timeout=300,
    )
    assert adotar.returncode == 0, adotar.stderr[-400:]

    conexao = sqlite3.connect(banco)
    assert list(conexao.execute("SELECT valor FROM parametros WHERE chave='teste'")) == [
        ("42",)
    ], "os dados do banco legado se perderam"
    assert list(conexao.execute("SELECT * FROM alembic_version")), "não foi carimbado"
    conexao.close()
