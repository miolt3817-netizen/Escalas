"""Aplicativo desktop — abre, usa, fecha.

Sem Docker, sem terminal, sem banco para instalar. Roda o sistema inteiro na
máquina com SQLite, abre o navegador sozinho e mostra o endereço da rede local
para que outras pessoas do quartel acessem do computador ou celular delas.

Onde ficam os dados (Windows):
    %APPDATA%\\EscalasBombeiros\\escalas.db

Fechar a janela encerra o sistema. Os dados permanecem para a próxima vez.
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

NOME = "Escalas BM — Escalas de Bombeiros"
PORTA_PADRAO = 8000


# --------------------------------------------------------------------------- #
# Pastas e configuração
# --------------------------------------------------------------------------- #


def pasta_de_dados() -> Path:
    """Pasta do usuário — sobrevive à atualização do aplicativo."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    destino = base / "EscalasBombeiros"
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def segredo_persistente(pasta: Path) -> str:
    """Gera o JWT_SECRET na primeira execução e reaproveita depois.

    Precisa ser estável entre execuções: se mudasse a cada abertura, todo mundo
    seria deslogado. E precisa ser único por instalação — um valor fixo no
    código permitiria forjar token de administrador em qualquer cópia.
    """
    arquivo = pasta / "chave.txt"
    if arquivo.exists():
        conteudo = arquivo.read_text(encoding="utf-8").strip()
        if conteudo:
            return conteudo
    chave = secrets.token_urlsafe(48)
    arquivo.write_text(chave, encoding="utf-8")
    if sys.platform != "win32":
        arquivo.chmod(0o600)
    return chave


def porta_livre(preferida: int = PORTA_PADRAO) -> int:
    for porta in (preferida, 8010, 8080, 8123, 0):
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", porta))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferida


def ip_da_rede() -> str | None:
    """IP da máquina na rede local, para o pessoal do quartel acessar."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.4)
            s.connect(("8.8.8.8", 80))  # não envia nada; só resolve a rota
            ip = s.getsockname()[0]
        return ip if not ip.startswith("127.") else None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Preparo do ambiente
# --------------------------------------------------------------------------- #


def preparar() -> tuple[Path, int]:
    pasta = pasta_de_dados()
    banco = pasta / "escalas.db"

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{banco}")
    os.environ.setdefault("JWT_SECRET", segredo_persistente(pasta))
    os.environ.setdefault("AMBIENTE", "desktop")

    primeira_vez = not banco.exists()
    if primeira_vez:
        # Senha inicial fixa só na primeira execução, para o usuário conseguir
        # entrar sem ler log nenhum. O sistema exige troca no primeiro acesso.
        os.environ.setdefault("SENHA_INICIAL", "bombeiros2026")

    from api.seed import main as semear

    semear()
    return banco, porta_livre()


def iniciar_servidor(porta: int) -> threading.Thread:
    import uvicorn

    from api.main import app

    config = uvicorn.Config(
        app, host="0.0.0.0", port=porta, log_level="warning", access_log=False
    )
    servidor = uvicorn.Server(config)
    tarefa = threading.Thread(target=servidor.run, daemon=True)
    tarefa.start()
    return tarefa


# --------------------------------------------------------------------------- #
# Janela de controle
# --------------------------------------------------------------------------- #


def abrir_janela(porta: int, banco: Path) -> None:
    import tkinter as tk
    from tkinter import font as tkfont

    local = f"http://localhost:{porta}"
    rede = ip_da_rede()
    endereco_rede = f"http://{rede}:{porta}" if rede else None

    janela = tk.Tk()
    janela.title(NOME)
    janela.configure(bg="#ffffff")
    janela.resizable(False, False)

    largura, altura = 460, 400 if endereco_rede else 340
    x = (janela.winfo_screenwidth() - largura) // 2
    y = (janela.winfo_screenheight() - altura) // 3
    janela.geometry(f"{largura}x{altura}+{x}+{y}")

    titulo = tkfont.Font(family="Segoe UI", size=15, weight="bold")
    corpo = tkfont.Font(family="Segoe UI", size=10)
    mono = tkfont.Font(family="Consolas", size=11)
    miudo = tkfont.Font(family="Segoe UI", size=9)

    tk.Frame(janela, bg="#d8181b", height=4).pack(fill="x")
    caixa = tk.Frame(janela, bg="#ffffff", padx=28, pady=22)
    caixa.pack(fill="both", expand=True)

    tk.Label(caixa, text="Escalas BM", font=titulo, bg="#ffffff", fg="#1c1d20").pack(
        anchor="w"
    )
    tk.Label(
        caixa,
        text="Sistema no ar. Pode usar.",
        font=corpo,
        bg="#ffffff",
        fg="#188038",
    ).pack(anchor="w", pady=(0, 16))

    def campo(rotulo: str, valor: str, dica: str = "") -> None:
        tk.Label(
            caixa, text=rotulo, font=miudo, bg="#ffffff", fg="#5f6368"
        ).pack(anchor="w")
        entrada = tk.Entry(
            caixa, font=mono, bd=1, relief="solid", bg="#f8f9fa", fg="#d8181b"
        )
        entrada.insert(0, valor)
        entrada.configure(state="readonly", readonlybackground="#f8f9fa")
        entrada.pack(fill="x", pady=(2, 2), ipady=5)
        if dica:
            tk.Label(
                caixa, text=dica, font=miudo, bg="#ffffff", fg="#80868b",
                wraplength=390, justify="left",
            ).pack(anchor="w", pady=(0, 12))
        else:
            tk.Frame(caixa, bg="#ffffff", height=12).pack()

    campo("NESTE COMPUTADOR", local)
    if endereco_rede:
        campo(
            "OUTROS APARELHOS NA MESMA REDE",
            endereco_rede,
            "Digite este endereço no navegador do celular ou de outro "
            "computador conectado ao mesmo Wi-Fi.",
        )

    botoes = tk.Frame(caixa, bg="#ffffff")
    botoes.pack(fill="x", pady=(4, 0))

    tk.Button(
        botoes, text="Abrir no navegador", command=lambda: webbrowser.open(local),
        font=corpo, bg="#d8181b", fg="#ffffff", bd=0, relief="flat",
        activebackground="#a91114", activeforeground="#ffffff",
        padx=18, pady=9, cursor="hand2",
    ).pack(side="left")

    tk.Button(
        botoes, text="Encerrar", command=janela.destroy,
        font=corpo, bg="#f1f3f4", fg="#5f6368", bd=0, relief="flat",
        padx=18, pady=9, cursor="hand2",
    ).pack(side="right")

    tk.Label(
        caixa,
        text=f"Dados salvos em {banco.parent}\nFechar esta janela encerra o sistema.",
        font=miudo, bg="#ffffff", fg="#80868b", justify="left",
    ).pack(anchor="w", pady=(16, 0))

    janela.after(600, lambda: webbrowser.open(local))
    janela.mainloop()


def modo_console(porta: int, banco: Path) -> None:
    """Quando não há interface gráfica disponível."""
    rede = ip_da_rede()
    print("\n" + "=" * 58)
    print(f"  {NOME}")
    print("=" * 58)
    print(f"  Neste computador:  http://localhost:{porta}")
    if rede:
        print(f"  Na rede local:     http://{rede}:{porta}")
    print(f"  Dados:             {banco.parent}")
    print("=" * 58)
    print("  Ctrl+C encerra.\n")
    try:
        webbrowser.open(f"http://localhost:{porta}")
    except Exception:  # noqa: BLE001 - servidor sem navegador
        pass
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("Encerrado.")


def main() -> None:
    banco, porta = preparar()
    iniciar_servidor(porta)
    try:
        import tkinter  # noqa: F401

        abrir_janela(porta, banco)
    except Exception:  # noqa: BLE001 - sem tkinter, cai no console
        modo_console(porta, banco)


if __name__ == "__main__":
    main()
