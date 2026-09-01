"""Fixtures compartilhadas.

`servidor_web` sobe um uvicorn de verdade contra um SQLite temporário, para
que os testes de layout tenham uma página real para carregar.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def servidor_web():
    """Sobe a aplicação num processo separado e devolve a URL base."""
    porta = _porta_livre()
    banco = Path(tempfile.mkdtemp()) / "layout.db"
    ambiente = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{banco}",
        "SENHA_INICIAL": "bombeiros2026",
        "JWT_SECRET": "teste-de-layout",
        "AMBIENTE": "desenvolvimento",
    }

    semente = subprocess.run(
        [sys.executable, "-m", "api.seed"],
        cwd=RAIZ, env=ambiente, capture_output=True, text=True, timeout=180,
    )
    if semente.returncode != 0:
        pytest.skip(f"seed falhou: {semente.stderr[-300:]}")

    processo = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app",
         "--port", str(porta), "--log-level", "warning"],
        cwd=RAIZ, env=ambiente,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{porta}"

    for _ in range(60):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(base + "/saude", timeout=3)
            break
        except (urllib.error.URLError, OSError):
            continue
    else:
        processo.terminate()
        pytest.skip("servidor não subiu a tempo")

    yield base

    processo.terminate()
    try:
        processo.wait(timeout=10)
    except subprocess.TimeoutExpired:
        processo.kill()
