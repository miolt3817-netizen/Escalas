"""Saldo de equidade — Parte 1, "Histórico inteligente e índice de equidade".

Regra central: o saldo é calculado por PROPORCIONALIDADE À DISPONIBILIDADE,
não por contagem bruta.

    esperado[b][c] = total_dias[c] × (elegiveis[b][c] / Σ elegiveis[c])
    saldo[b][c]    = realizado[b][c] − esperado[b][c]

Motivo: com contagem bruta, quem tirou 30 dias de férias volta com déficit e é
sobrecarregado para "empatar" — punindo o exercício de um direito. Com o cálculo
proporcional, o período de férias não conta como dia elegível e o bombeiro
retorna com saldo neutro.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from .dominio import (
    Bombeiro,
    Categoria,
    EntradaSolve,
    Indisponibilidade,
    Plantao,
)


def esta_disponivel(
    bombeiro_id: int, dia: date, indisponibilidades: list[Indisponibilidade]
) -> bool:
    return not any(
        i.bombeiro_id == bombeiro_id and i.cobre(dia) for i in indisponibilidades
    )


def indice_de_ausencias(
    indisponibilidades: list[Indisponibilidade], dias: Iterable[date]
) -> set[tuple[int, date]]:
    """Pares (bombeiro, dia) indisponíveis, para consulta em tempo constante.

    A varredura linear por (bombeiro, dia) custava caro quando somada: com 39
    bombeiros, 6 categorias, 31 dias e 21 meses de histórico eram milhões de
    comparações só para montar um painel. Indexar uma vez resolve.
    """
    unicos = sorted(set(dias))
    return {
        (i.bombeiro_id, d)
        for i in indisponibilidades
        for d in unicos
        if i.inicio <= d <= i.fim
    }


def dias_elegiveis(
    bombeiros: list[Bombeiro],
    dias_da_categoria: dict[Categoria, list[date]],
    indisponibilidades: list[Indisponibilidade],
) -> dict[int, dict[Categoria, int]]:
    """Quantos dias de cada categoria o bombeiro poderia ter trabalhado."""
    todos = [d for dias in dias_da_categoria.values() for d in dias]
    ausente = indice_de_ausencias(indisponibilidades, todos)
    return {
        b.id: {
            cat: sum(1 for d in dias if (b.id, d) not in ausente)
            for cat, dias in dias_da_categoria.items()
        }
        for b in bombeiros
    }


def carga_esperada(
    bombeiros: list[Bombeiro],
    dias_da_categoria: dict[Categoria, list[date]],
    elegiveis: dict[int, dict[Categoria, int]],
) -> dict[int, dict[Categoria, float]]:
    """Parcela justa de cada bombeiro em cada categoria, no período."""
    saida: dict[int, dict[Categoria, float]] = {b.id: {} for b in bombeiros}
    for cat, dias in dias_da_categoria.items():
        total_dias = len(dias)
        soma_elegiveis = sum(elegiveis[b.id][cat] for b in bombeiros)
        for b in bombeiros:
            if soma_elegiveis == 0:
                saida[b.id][cat] = 0.0
            else:
                saida[b.id][cat] = (
                    total_dias * elegiveis[b.id][cat] / soma_elegiveis
                )
    return saida


def realizado(
    bombeiros: list[Bombeiro], plantoes: list[Plantao]
) -> dict[int, dict[Categoria, int]]:
    saida: dict[int, dict[Categoria, int]] = {
        b.id: {c: 0 for c in Categoria} for b in bombeiros
    }
    for p in plantoes:
        if p.bombeiro_id not in saida:
            continue
        for cat in p.categorias:
            saida[p.bombeiro_id][cat] += 1
    return saida


def saldo_do_periodo(
    bombeiros: list[Bombeiro],
    plantoes: list[Plantao],
    dias_da_categoria: dict[Categoria, list[date]],
    indisponibilidades: list[Indisponibilidade],
) -> dict[int, dict[Categoria, float]]:
    """Saldo gerado apenas por este período (sem o histórico anterior)."""
    elegiveis = dias_elegiveis(bombeiros, dias_da_categoria, indisponibilidades)
    esperado = carga_esperada(bombeiros, dias_da_categoria, elegiveis)
    feito = realizado(bombeiros, plantoes)
    return {
        b.id: {
            cat: feito[b.id][cat] - esperado[b.id].get(cat, 0.0)
            for cat in dias_da_categoria
        }
        for b in bombeiros
    }


def saldo_acumulado(
    entrada: EntradaSolve,
    plantoes: list[Plantao],
    dias_da_categoria: dict[Categoria, list[date]],
) -> dict[int, dict[Categoria, float]]:
    """Histórico + período corrente — é ISTO que o algoritmo equaliza.

    Ver Apêndice A, correção nº 1: a compensação histórica não é um estágio
    separado no fim da fila; ela está embutida em cada métrica de equalização.
    """
    bombeiros = entrada.bombeiros_ativos()
    do_periodo = saldo_do_periodo(
        bombeiros, plantoes, dias_da_categoria, entrada.indisponibilidades
    )
    return {
        b.id: {
            cat: entrada.saldo_de(b.id, cat) + do_periodo[b.id].get(cat, 0.0)
            for cat in dias_da_categoria
        }
        for b in bombeiros
    }


def amplitude(saldos: dict[int, dict[Categoria, float]], cat: Categoria) -> float:
    """Métrica de equalização adotada (Parte 0.6): máximo − mínimo."""
    valores = [s.get(cat, 0.0) for s in saldos.values()]
    if not valores:
        return 0.0
    return max(valores) - min(valores)
