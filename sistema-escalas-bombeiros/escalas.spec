# -*- mode: python ; coding: utf-8 -*-
"""Empacotamento do aplicativo desktop.

    pyinstaller escalas.spec --noconfirm

Gera `dist/Escalas/` — uma pasta com o executável e as dependências.

Por que pasta e não arquivo único: o OR-Tools carrega bibliotecas nativas em
tempo de execução. Em modo `--onefile` o PyInstaller descompacta tudo numa
pasta temporária a cada abertura, o que deixa a inicialização lenta e às vezes
quebra o carregamento dessas bibliotecas. A pasta abre rápido e é confiável.
"""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# --------------------------------------------------------------------------- #
# Dependências que o PyInstaller não descobre sozinho
# --------------------------------------------------------------------------- #

# OR-Tools: bibliotecas nativas (.so/.dll/.pyd) carregadas em tempo de execução.
binarios = collect_dynamic_libs("ortools")
dados = collect_data_files("ortools")

# openpyxl embute planilhas de referência como dados.
dados += collect_data_files("openpyxl")

# A interface e o SQL de infraestrutura precisam viajar junto.
# `web` inclui index.html, manifest.json, sw.js e a pasta estatico/ com os
# ícones do aplicativo instalável.
dados += [
    ("web", "web"),
    ("infra", "infra"),
]

# Módulos alcançados só por importação dinâmica.
ocultos = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.postgresql",
    "bcrypt",
    "jose.backends.cryptography_backend",
    "email_validator",
    "openpyxl",
    "api",
    "api.main",
    "api.seed",
    "api.exportacao",
    "motor.solver",
]
ocultos += collect_submodules("ortools")


a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=binarios,
    datas=dados,
    hiddenimports=ocultos,
    hookspath=[],
    runtime_hooks=[],
    # WeasyPrint depende de bibliotecas GTK que não existem no Windows sem
    # instalação separada. O PDF é gerado por ReportLab quando ela falta —
    # ver api/exportacao.py.
    excludes=["weasyprint", "tkinter.test", "test", "unittest", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EscalasBM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # sem janela preta de terminal
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="EscalasBM",
)
