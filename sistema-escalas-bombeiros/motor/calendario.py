"""Classificação de dias — Parte 0.3 e Parte 1, "Escalas".

Um plantão de 24h iniciado na sexta cobre metade do sábado. O critério adotado
(configurável) é classificar pelo dia em que o plantão COMEÇA.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from .dominio import Categoria, Feriado, Parametros


def dias_do_mes(ano: int, mes: int) -> list[date]:
    _, ultimo = calendar.monthrange(ano, mes)
    return [date(ano, mes, d) for d in range(1, ultimo + 1)]


def dias_anteriores(ano: int, mes: int, quantidade: int) -> list[date]:
    """Últimos N dias do mês anterior — usados na continuidade entre meses."""
    primeiro = date(ano, mes, 1)
    return [primeiro - timedelta(days=i) for i in range(quantidade, 0, -1)]


def categorias_do_dia(
    dia: date, feriados: set[date], parametros: Parametros | None = None
) -> tuple[Categoria, ...]:
    """Categorias às quais um dia pertence. Um dia pode pertencer a várias.

    Sábado que também é feriado conta em SABADO, FERIADO e VERMELHA.
    """
    del parametros  # criterio "inicio": o dia do calendário é o dia do plantão
    cats: list[Categoria] = [Categoria.TOTAL]
    eh_feriado = dia in feriados
    dia_semana = dia.weekday()  # 0=seg ... 5=sab, 6=dom
    eh_fim_de_semana = dia_semana >= 5

    if eh_fim_de_semana or eh_feriado:
        cats.append(Categoria.VERMELHA)
    else:
        cats.append(Categoria.BRANCA)

    if dia_semana == 5:
        cats.append(Categoria.SABADO)
    if dia_semana == 6:
        cats.append(Categoria.DOMINGO)
    if eh_feriado:
        cats.append(Categoria.FERIADO)

    return tuple(cats)


def tipo_do_dia(dia: date, feriados: set[date]) -> str:
    cats = categorias_do_dia(dia, feriados)
    return "vermelha" if Categoria.VERMELHA in cats else "branca"


def mapa_categorias(
    dias: list[date], feriados: list[Feriado], parametros: Parametros | None = None
) -> dict[date, tuple[Categoria, ...]]:
    conjunto = {f.data for f in feriados}
    return {d: categorias_do_dia(d, conjunto, parametros) for d in dias}


def dias_por_categoria(
    mapa: dict[date, tuple[Categoria, ...]],
) -> dict[Categoria, list[date]]:
    saida: dict[Categoria, list[date]] = {c: [] for c in Categoria}
    for dia, cats in mapa.items():
        for c in cats:
            saida[c].append(dia)
    for c in saida:
        saida[c].sort()
    return saida
