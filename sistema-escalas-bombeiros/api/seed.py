"""Carga inicial — cria tabelas, aplica infra/init.sql e semeia dados.

Idempotente: pode rodar a cada subida do container.

A equipe semeada é fictícia mas plausível: dez bombeiros com férias espalhadas
pelo ano, alguns afastamentos, preferências variadas, e alguns meses de escala
já publicados. Esse histórico é o que faz o saldo de equidade aparecer com
números reais em vez de zeros — sem ele, quem abre o sistema pela primeira vez
não consegue ver a compensação histórica funcionando, que é o diferencial do
produto.
"""

from __future__ import annotations

import os
import secrets
import string
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select, text

from . import modelos as m
from .banco import Sessao, criar_tabelas, definir_contexto_auditoria, engine
from .seguranca import hash_senha

INIT_SQL = Path(__file__).resolve().parent.parent / "infra" / "init.sql"

#: Meses de histórico gerados e publicados na primeira carga.
MESES_HISTORICO = 3

PARAMETROS_PADRAO = [
    ("duracao_plantao_horas", "24", "0.1 — duração do plantão"),
    ("hora_inicio", "8", "0.1 — hora de início do plantão"),
    ("intervalo_minimo_dias", "1", "0.2 — folga obrigatória entre plantões"),
    ("intervalo_desejavel_dias", "3", "0.4 — folga desejável (soft, estágio E1)"),
    ("criterio_classificacao", "inicio", "0.3 — classifica pelo dia de início"),
    ("escala_inteira", "100", "0.6 — fator float->inteiro para o CP-SAT"),
    ("epsilon_equidade", "50", "folga ao travar estágios de equidade"),
    ("tempo_limite_estagio_s", "10", "limite por estágio, em segundos"),
    ("random_seed", "0", "determinismo para auditoria"),
    ("num_workers", "1", "determinismo para auditoria"),
]

FERIADOS = [
    (date(2026, 1, 1), "Confraternização Universal"),
    (date(2026, 2, 16), "Carnaval"),
    (date(2026, 2, 17), "Carnaval"),
    (date(2026, 4, 3), "Sexta-feira Santa"),
    (date(2026, 4, 21), "Tiradentes"),
    (date(2026, 5, 1), "Dia do Trabalho"),
    (date(2026, 6, 4), "Corpus Christi"),
    (date(2026, 9, 7), "Independência"),
    (date(2026, 10, 12), "Nossa Senhora Aparecida"),
    (date(2026, 11, 2), "Finados"),
    (date(2026, 11, 15), "Proclamação da República"),
    (date(2026, 11, 20), "Consciência Negra"),
    (date(2026, 12, 25), "Natal"),
    (date(2027, 1, 1), "Confraternização Universal"),
    (date(2027, 2, 8), "Carnaval"),
    (date(2027, 2, 9), "Carnaval"),
    (date(2027, 3, 26), "Sexta-feira Santa"),
    (date(2027, 4, 21), "Tiradentes"),
    (date(2027, 5, 1), "Dia do Trabalho"),
    (date(2027, 5, 27), "Corpus Christi"),
    (date(2027, 9, 7), "Independência"),
    (date(2027, 10, 12), "Nossa Senhora Aparecida"),
    (date(2027, 11, 2), "Finados"),
    (date(2027, 11, 15), "Proclamação da República"),
    (date(2027, 12, 25), "Natal"),
]

#: (nome, usuário do e-mail, ausências, preferências)
#: Ausência:    (início, fim, tipo)
#: Preferência: (tipo, data, dia_semana, peso) — 0 = segunda ... 6 = domingo
#: (nome, usuário do e-mail, ausências, preferências, ativo)
#: Ausência:    (início, fim, tipo)
#: Preferência: (tipo, data, dia_semana, peso) — 0 = segunda ... 6 = domingo
#:
#: Equipe fictícia, com histórias plausíveis: férias espalhadas pelo ano (a
#: corporação não pode ter todo mundo fora no mesmo mês), licenças e atestados
#: de durações diferentes, e preferências que refletem vida real. O comentário
#: ao lado de cada preferência é o motivo humano por trás dela.
EQUIPE = [
    (
        "Anderson Duarte Prates", "anderson.prates",
        [
            (date(2026, 1, 12), date(2026, 2, 10), "ferias"),
            (date(2026, 7, 20), date(2026, 7, 22), "atestado"),
        ],
        [("evita", None, 6, 2)],          # cursa Enfermagem aos domingos
        True,
    ),
    (
        "Cristiane Balbinot Sfredo", "cristiane.sfredo",
        [(date(2026, 3, 2), date(2026, 3, 31), "ferias")],
        [("quer", None, 5, 1), ("evita", None, 2, 1)],  # sábado rende; quarta é dia do filho
        True,
    ),
    (
        "Everton Kaufmann Reis", "everton.reis",
        [
            (date(2026, 5, 4), date(2026, 6, 2), "ferias"),
            (date(2026, 9, 14), date(2026, 9, 25), "licenca"),  # licença paternidade
        ],
        [("evita", None, 0, 1)],          # segunda leva a filha à fisioterapia
        True,
    ),
    (
        "Gabriela Nunes Tavares", "gabriela.tavares",
        [(date(2026, 7, 6), date(2026, 8, 4), "ferias")],
        [("evita", None, 4, 2), ("evita", None, 5, 2)],  # mora em outra cidade, viaja no fim de semana
        True,
    ),
    (
        "Henrique Bortolini Vaz", "henrique.vaz",
        [
            (date(2026, 2, 9), date(2026, 3, 10), "ferias"),
            (date(2026, 11, 3), date(2026, 11, 7), "atestado"),
        ],
        [("quer", None, 6, 1)],           # domingo tem adicional
        True,
    ),
    (
        "Juliana Weber Colombo", "juliana.colombo",
        [
            (date(2026, 10, 5), date(2026, 11, 3), "ferias"),
            (date(2026, 4, 13), date(2026, 5, 12), "licenca"),  # licença médica prolongada
        ],
        [("evita", None, 1, 1)],
        True,
    ),
    (
        "Leandro Piccoli Machado", "leandro.machado",
        [(date(2026, 6, 8), date(2026, 7, 7), "ferias")],
        [],                               # sem preferência declarada
        True,
    ),
    (
        "Michele Grando Sartori", "michele.sartori",
        [
            (date(2026, 12, 7), date(2027, 1, 5), "ferias"),
            (date(2026, 8, 24), date(2026, 8, 26), "atestado"),
        ],
        [("quer", None, 3, 1)],           # quinta o marido fica com as crianças
        True,
    ),
    (
        "Rodrigo Bavaresco Milani", "rodrigo.milani",
        [(date(2026, 4, 6), date(2026, 5, 5), "ferias")],
        [("evita", None, 1, 1), ("evita", None, 3, 1)],  # treina para concurso
        True,
    ),
    (
        "Simone Pagnussat Oliveira", "simone.oliveira",
        [
            (date(2026, 8, 10), date(2026, 9, 8), "ferias"),
            (date(2026, 11, 23), date(2026, 12, 4), "afastamento"),
        ],
        [("evita", None, 5, 2), ("evita", None, 6, 2)],  # cuida da mãe no fim de semana
        True,
    ),
    (
        "Vilmar Chiapinotto Reck", "vilmar.reck",
        [],
        [],
        False,   # aposentou em janeiro; mantido inativo para o histórico fechar
    ),
]

DOMINIO = "cb.sc.gov.br"


def gerar_senha(tamanho: int = 12) -> str:
    """Senha aleatória legível: sem caracteres ambíguos (l, I, 1, O, 0)."""
    alfabeto = (
        "".join(c for c in string.ascii_letters if c not in "lIO")
        + "".join(d for d in string.digits if d not in "01")
    )
    return "".join(secrets.choice(alfabeto) for _ in range(tamanho))


def aplicar_init_sql() -> None:
    """Triggers de auditoria e view materializada — só no Postgres."""
    if not str(engine.url).startswith("postgresql"):
        print("[seed] banco não-Postgres: auditoria feita pelo ORM.")
        return
    if not INIT_SQL.exists():
        print("[seed] infra/init.sql não encontrado.")
        return
    with engine.begin() as conexao:
        conexao.execute(text(INIT_SQL.read_text(encoding="utf-8")))
    print("[seed] init.sql aplicado (auditoria + vw_equidade).")


def _semear_calendario(db) -> None:
    for data, nome in FERIADOS:
        if not db.scalars(select(m.Feriado).where(m.Feriado.data == data)).first():
            db.add(m.Feriado(data=data, nome=nome))
    for chave, valor, descricao in PARAMETROS_PADRAO:
        if db.get(m.Parametro, chave) is None:
            db.add(m.Parametro(chave=chave, valor=valor, descricao=descricao))
    db.commit()


def _semear_usuarios(db) -> list[tuple[str, str, str]]:
    senha_fixa = os.getenv("SENHA_INICIAL", "")
    criadas: list[tuple[str, str, str]] = []

    contas = [
        ("Administrador do Sistema", f"admin@{DOMINIO}", "administrador"),
        ("Sgt. Roberto Nascimento", f"supervisor@{DOMINIO}", "supervisor"),
    ] + [
        (nome, f"{usuario}@{DOMINIO}", "bombeiro")
        for nome, usuario, _, _, _ in EQUIPE
    ]

    for nome, email, papel in contas:
        if db.scalars(select(m.Usuario).where(m.Usuario.email == email)).first():
            continue
        senha = senha_fixa or gerar_senha()
        db.add(
            m.Usuario(
                nome=nome,
                email=email,
                senha_hash=hash_senha(senha),
                papel=papel,
                precisa_trocar_senha=True,
            )
        )
        criadas.append((nome, email, senha))
    db.commit()
    return criadas


def _semear_ausencias_e_preferencias(db) -> None:
    """Férias, licenças e preferências — o que torna a demonstração realista."""
    if db.scalars(select(m.Indisponibilidade)).first():
        return  # já semeado antes

    for nome, usuario, ausencias, preferencias, ativo in EQUIPE:
        pessoa = db.scalars(
            select(m.Usuario).where(m.Usuario.email == f"{usuario}@{DOMINIO}")
        ).first()
        if pessoa is None:
            continue
        if not ativo:
            # Bombeiro que saiu da corporação: fica inativo, sem entrar em
            # escala nova, mas com o cadastro preservado para o histórico.
            pessoa.ativo = False
        for inicio, fim, tipo in ausencias:
            db.add(
                m.Indisponibilidade(
                    bombeiro_id=pessoa.id, inicio=inicio, fim=fim, tipo=tipo
                )
            )
        for tipo, data, dia_semana, peso in preferencias:
            db.add(
                m.Preferencia(
                    bombeiro_id=pessoa.id,
                    tipo=tipo,
                    data=data,
                    dia_semana=dia_semana,
                    peso=peso,
                )
            )
    db.commit()


def _mes_anterior(referencia: date, quantidade: int) -> date:
    """Primeiro dia do mês N meses antes da referência."""
    ano, mes = referencia.year, referencia.month
    total = (ano * 12 + mes - 1) - quantidade
    return date(total // 12, total % 12 + 1, 1)


def _semear_historico(db) -> int:
    """Gera e publica os meses anteriores ao atual.

    Sem histórico, o painel de equidade abre zerado e a compensação — que é o
    que diferencia esta ferramenta de uma planilha — fica invisível para quem
    está avaliando o sistema.
    """
    from . import servicos as s

    if db.scalars(select(m.Escala).where(m.Escala.status == "publicada")).first():
        return 0

    supervisor = db.scalars(
        select(m.Usuario).where(m.Usuario.papel == "supervisor")
    ).first()
    autor = supervisor.id if supervisor else None

    hoje = date.today()
    publicados = 0
    # Inclui o mês corrente (atras = 0): sem ele, todos os dias publicados
    # estariam no passado e ninguém conseguiria experimentar substituição,
    # troca ou ajuste — que só valem para plantão que ainda não aconteceu.
    for atras in range(MESES_HISTORICO, -1, -1):
        alvo = _mes_anterior(hoje, atras)
        definir_contexto_auditoria(db, autor, "carga inicial de histórico")
        escala, _resultado, _texto = s.gerar_escala(db, alvo.year, alvo.month, autor)
        if escala is None:
            print(
                f"[seed] {alvo.month:02d}/{alvo.year} sem solução viável; "
                "seguindo sem esse mês."
            )
            continue
        s.publicar(db, escala.id)
        publicados += 1
    return publicados


def semear() -> None:
    db = Sessao()
    try:
        _semear_calendario(db)
        criadas = _semear_usuarios(db)
        _semear_ausencias_e_preferencias(db)

        if criadas:
            print("\n" + "=" * 72)
            print("  SENHAS INICIAIS — anote agora, não são exibidas de novo.")
            print("  Cada usuário escolhe a própria senha no primeiro acesso.")
            print("=" * 72)
            for nome, email, senha in criadas:
                print(f"  {nome:<26} {email:<32} {senha}")
            print("=" * 72 + "\n")
        else:
            print("[seed] usuários já existiam; nenhuma senha foi alterada.")

        publicados = _semear_historico(db)
        if publicados:
            print(
                f"[seed] {publicados} mês(es) de escala gerados e publicados — "
                "o mês corrente já abre pronto e o saldo tem histórico."
            )
    finally:
        db.close()


def main() -> None:
    criar_tabelas()
    aplicar_init_sql()
    semear()


if __name__ == "__main__":
    main()
