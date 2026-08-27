"""Estágios de otimização lexicográfica — Parte 1, "Algoritmo".

Seis estágios reais, não onze níveis:

    E1  espaçamento além do descanso obrigatório      (Parte 0.4)
    E2  equalizar carga TOTAL acumulada
    E3  equalizar escala BRANCA acumulada
    E4  equalizar escala VERMELHA acumulada
    E5  equalizar SÁBADO, depois DOMINGO, depois FERIADO (acumulados)
    E6  atender preferências

"Cobrir todos os dias" e "respeitar restrições obrigatórias" são restrições
hard (H1–H4), não objetivos — por isso não geram estágio. "Distribuir
igualmente os plantões" é E2. Sábado/domingo/feriado são refinamentos dentro
de E5.

Toda equalização opera sobre o saldo ACUMULADO (histórico + mês corrente),
nunca sobre a contagem do mês isolado. Ver Apêndice A, correção nº 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ortools.sat.python import cp_model

from .dominio import Categoria, TipoPreferencia
from .modelo import Contexto


@dataclass
class Estagio:
    codigo: str
    descricao: str
    #: Constrói a expressão objetivo (sempre de MINIMIZAÇÃO).
    construir_objetivo: Callable[[Contexto], object]
    #: Folga permitida ao travar este estágio como restrição do seguinte.
    epsilon: Callable[[Contexto], int]
    #: Formata o valor inteiro do solver para leitura humana.
    formatar: Callable[[Contexto, int], str]


# --------------------------------------------------------------------------- #
# E1 — espaçamento
# --------------------------------------------------------------------------- #


def _objetivo_espacamento(ctx: Contexto):
    """Penaliza pares de plantões do mesmo bombeiro separados por menos que o
    intervalo desejável. O intervalo mínimo já é hard (H3); aqui trata-se de
    evitar, por exemplo, dia 10 e dia 12."""
    p = ctx.entrada.parametros
    minimo = p.intervalo_minimo_dias
    desejavel = p.intervalo_desejavel_dias
    if desejavel <= minimo:
        return 0

    penalidades = []
    for b in ctx.bombeiro_ids:
        for i, dia in enumerate(ctx.dias):
            for k in range(minimo + 1, desejavel + 1):
                if i + k >= len(ctx.dias):
                    continue
                outro = ctx.dias[i + k]
                y = ctx.modelo.NewBoolVar(f"prox_{b}_{i}_{k}")
                ctx.modelo.AddBoolAnd([ctx.x[(b, dia)], ctx.x[(b, outro)]]).OnlyEnforceIf(y)
                ctx.modelo.AddBoolOr(
                    [ctx.x[(b, dia)].Not(), ctx.x[(b, outro)].Not()]
                ).OnlyEnforceIf(y.Not())
                penalidades.append(y)
    return sum(penalidades) if penalidades else 0


# --------------------------------------------------------------------------- #
# E2..E5 — equalização por categoria
# --------------------------------------------------------------------------- #


def _limite_saldo(ctx: Contexto) -> int:
    """Limite folgado para as variáveis de saldo, em unidades escaladas."""
    e = ctx.escala
    maior_hist = 0
    for bid in ctx.bombeiro_ids:
        for cat in Categoria:
            maior_hist = max(maior_hist, abs(ctx.entrada.saldo_de(bid, cat)))
    return int((len(ctx.dias) + maior_hist + 2) * e * 2)


def _objetivo_equalizar(cat: Categoria):
    def construir(ctx: Contexto):
        dias_cat = ctx.dias_de(cat)
        if not dias_cat or len(ctx.bombeiro_ids) < 2:
            return 0

        limite = _limite_saldo(ctx)
        saldos = []
        for bid in ctx.bombeiro_ids:
            v = ctx.modelo.NewIntVar(-limite, limite, f"saldo_{cat.value}_{bid}")
            ctx.modelo.Add(v == ctx.saldo_expr(bid, cat))
            saldos.append(v)

        maior = ctx.modelo.NewIntVar(-limite, limite, f"max_{cat.value}")
        menor = ctx.modelo.NewIntVar(-limite, limite, f"min_{cat.value}")
        ctx.modelo.AddMaxEquality(maior, saldos)
        ctx.modelo.AddMinEquality(menor, saldos)

        amplitude = ctx.modelo.NewIntVar(0, 2 * limite, f"ampl_{cat.value}")
        ctx.modelo.Add(amplitude == maior - menor)
        return amplitude

    return construir


# --------------------------------------------------------------------------- #
# E6 — preferências
# --------------------------------------------------------------------------- #


def _objetivo_preferencias(ctx: Contexto):
    """Minimiza o peso das preferências NÃO atendidas."""
    termos = []
    for pref in ctx.entrada.preferencias:
        if pref.bombeiro_id not in ctx.bombeiro_ids:
            continue
        for dia in pref.alvos(ctx.dias):
            var = ctx.x[(pref.bombeiro_id, dia)]
            if pref.tipo == TipoPreferencia.QUER:
                termos.append(pref.peso * (1 - var))
            else:
                termos.append(pref.peso * var)
    return sum(termos) if termos else 0


def contar_preferencias(ctx: Contexto, escala: dict) -> tuple[int, int]:
    atendidas = total = 0
    for pref in ctx.entrada.preferencias:
        if pref.bombeiro_id not in ctx.bombeiro_ids:
            continue
        for dia in pref.alvos(ctx.dias):
            total += 1
            escalado = escala.get(dia) == pref.bombeiro_id
            if (pref.tipo == TipoPreferencia.QUER) == escalado:
                atendidas += 1
    return atendidas, total


# --------------------------------------------------------------------------- #
# Montagem da sequência
# --------------------------------------------------------------------------- #


def _fmt_plantoes(ctx: Contexto, valor: int) -> str:
    return f"{valor / ctx.escala:.2f} plantão(ões)"


def _fmt_bruto(ctx: Contexto, valor: int) -> str:
    del ctx
    return str(valor)


_ROTULOS = {
    Categoria.TOTAL: ("E2", "Equalizar carga total acumulada"),
    Categoria.BRANCA: ("E3", "Equalizar escala branca acumulada"),
    Categoria.VERMELHA: ("E4", "Equalizar escala vermelha acumulada"),
    Categoria.SABADO: ("E5a", "Equalizar sábados acumulados"),
    Categoria.DOMINGO: ("E5b", "Equalizar domingos acumulados"),
    Categoria.FERIADO: ("E5c", "Equalizar feriados acumulados"),
}


def sequencia_padrao() -> list[Estagio]:
    estagios = [
        Estagio(
            codigo="E1",
            descricao="Maximizar espaçamento entre plantões do mesmo bombeiro",
            construir_objetivo=_objetivo_espacamento,
            epsilon=lambda c: c.entrada.parametros.epsilon_espacamento,
            formatar=lambda c, v: f"{v} par(es) abaixo do intervalo desejável",
        )
    ]

    for cat in (
        Categoria.TOTAL,
        Categoria.BRANCA,
        Categoria.VERMELHA,
        Categoria.SABADO,
        Categoria.DOMINGO,
        Categoria.FERIADO,
    ):
        codigo, descricao = _ROTULOS[cat]
        estagios.append(
            Estagio(
                codigo=codigo,
                descricao=descricao,
                construir_objetivo=_objetivo_equalizar(cat),
                epsilon=lambda c: c.entrada.parametros.epsilon_equidade,
                formatar=_fmt_plantoes,
            )
        )

    estagios.append(
        Estagio(
            codigo="E6",
            descricao="Atender preferências",
            construir_objetivo=_objetivo_preferencias,
            epsilon=lambda c: c.entrada.parametros.epsilon_preferencias,
            formatar=lambda c, v: f"{v} ponto(s) de preferência não atendidos",
        )
    )
    return estagios
