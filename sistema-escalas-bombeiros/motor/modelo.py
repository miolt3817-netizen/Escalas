"""Modelo CP-SAT — Parte 2, "Motor de otimização".

Variáveis:
    x[b][d] ∈ {0,1}  — bombeiro b escalado no dia d

Restrições obrigatórias (hard, nunca pesos altos):
    (H1) ∀d:  Σ_b x[b][d] = 1
    (H2) ∀b, ∀d ∈ indisponível(b):  x[b][d] = 0
    (H3) ∀b, ∀d:  Σ_{k=0..folga} x[b][d+k] ≤ 1        (Parte 0.2)
    (H4) ∀(b,d) ∈ fixados:  x[b][d] = valor_fixado

H3 atravessa a virada do mês: os últimos dias do mês anterior entram como
constantes (Parte 2, "Continuidade entre meses").

As restrições relaxáveis recebem literais de enforcement registrados como
assumptions, para que `SufficientAssumptionsForInfeasibility()` devolva o
conjunto mínimo de conflito quando não houver solução.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ortools.sat.python import cp_model

from .calendario import dias_do_mes, dias_por_categoria, mapa_categorias
from .dominio import Categoria, EntradaSolve
from .equidade import carga_esperada, dias_elegiveis, esta_disponivel


@dataclass
class Contexto:
    """Tudo que os estágios de otimização precisam para montar objetivos."""

    entrada: EntradaSolve
    modelo: cp_model.CpModel
    x: dict[tuple[int, date], cp_model.IntVar]
    dias: list[date]
    bombeiro_ids: list[int]
    mapa_cats: dict[date, tuple[Categoria, ...]]
    dias_da_categoria: dict[Categoria, list[date]]
    esperado: dict[int, dict[Categoria, float]]
    assumptions: dict[cp_model.IntVar, str] = field(default_factory=dict)

    @property
    def escala(self) -> int:
        return self.entrada.parametros.escala_inteira

    def dias_de(self, cat: Categoria) -> list[date]:
        return [d for d in self.dias_da_categoria.get(cat, []) if d in set(self.dias)]

    def saldo_expr(self, bombeiro_id: int, cat: Categoria):
        """Expressão inteira do saldo acumulado final (histórico + mês).

            saldo_final = saldo_hist×E + E×Σ x[b][d] − round(esperado×E)
        """
        e = self.escala
        hist = round(self.entrada.saldo_de(bombeiro_id, cat) * e)
        esperado = round(self.esperado[bombeiro_id].get(cat, 0.0) * e)
        termos = [self.x[(bombeiro_id, d)] for d in self.dias_de(cat)]
        return sum(e * t for t in termos) + (hist - esperado)


def construir(entrada: EntradaSolve) -> Contexto:
    entrada.parametros.validar()
    p = entrada.parametros
    bombeiros = entrada.bombeiros_ativos()
    if not bombeiros:
        raise ValueError("Nenhum bombeiro ativo — impossível gerar escala.")

    dias = dias_do_mes(entrada.ano, entrada.mes)
    mapa_cats = mapa_categorias(dias, entrada.feriados, p)
    por_cat = dias_por_categoria(mapa_cats)

    modelo = cp_model.CpModel()
    x: dict[tuple[int, date], cp_model.IntVar] = {}
    for b in bombeiros:
        for d in dias:
            x[(b.id, d)] = modelo.NewBoolVar(f"x_{b.id}_{d.isoformat()}")

    assumptions: dict[cp_model.IntVar, str] = {}

    def com_assumption(rotulo: str) -> cp_model.IntVar:
        lit = modelo.NewBoolVar(f"assume_{len(assumptions)}")
        assumptions[lit] = rotulo
        modelo.AddAssumption(lit)
        return lit

    # ---------------------------------------------------------------- H1
    # Exatamente um bombeiro por dia. Não é relaxável: é a demanda do serviço.
    for d in dias:
        modelo.Add(sum(x[(b.id, d)] for b in bombeiros) == 1)

    # ---------------------------------------------------------------- H2
    # Indisponibilidades. Uma assumption por bloco, para que o conjunto mínimo
    # de conflito aponte "férias do Silva", não 30 restrições soltas.
    for indisp in entrada.indisponibilidades:
        alvo = [
            d
            for d in dias
            if indisp.cobre(d) and (indisp.bombeiro_id, d) in x
        ]
        if not alvo:
            continue
        rotulo = (
            f"{indisp.tipo.value} do bombeiro {indisp.bombeiro_id} "
            f"({indisp.inicio.isoformat()} a {indisp.fim.isoformat()})"
        )
        lit = com_assumption(rotulo)
        for d in alvo:
            modelo.Add(x[(indisp.bombeiro_id, d)] == 0).OnlyEnforceIf(lit)

    # ---------------------------------------------------------------- H3
    # Descanso mínimo, incluindo a virada do mês.
    folga = p.intervalo_minimo_dias
    if folga > 0:
        lit_descanso = com_assumption(
            f"regra de descanso mínimo ({folga} dia(s) de folga entre plantões)"
        )
        janela = folga + 1
        anteriores = {(pa.bombeiro_id, pa.data) for pa in entrada.plantoes_anteriores}
        primeiro = dias[0]

        # Pares liberados pelo supervisor. A janela que contém um deles deixa
        # de valer — mas só ela, e só para aquele bombeiro naquele dia.
        liberados = set(entrada.excecoes_descanso)

        for b in bombeiros:
            # janelas internas ao mês
            for i in range(len(dias)):
                bloco = dias[i : i + janela]
                if len(bloco) < 2:
                    continue
                if any((b.id, d) in liberados for d in bloco):
                    continue
                modelo.Add(
                    sum(x[(b.id, d)] for d in bloco) <= 1
                ).OnlyEnforceIf(lit_descanso)

            # continuidade com o mês anterior
            for k in range(1, janela):
                dia_anterior = primeiro - timedelta(days=k)
                if (b.id, dia_anterior) in anteriores and not any(
                    (b.id, dias[j]) in liberados for j in range(min(janela, len(dias)))
                ):
                    # trabalhou no fim do mês passado: bloqueia os primeiros dias
                    for j in range(janela - k):
                        if j < len(dias):
                            modelo.Add(x[(b.id, dias[j])] == 0).OnlyEnforceIf(
                                lit_descanso
                            )

    # ---------------------------------------------------------------- H4
    # Plantões travados (decorridos, ajuste manual, troca aprovada).
    for fix in entrada.fixados:
        if fix.data not in dias:
            continue
        if (fix.bombeiro_id, fix.data) not in x:
            raise ValueError(
                f"Plantão fixado para bombeiro inexistente/inativo: {fix.bombeiro_id}"
            )
        lit = com_assumption(
            f"plantão travado em {fix.data.isoformat()} "
            f"(bombeiro {fix.bombeiro_id}, {fix.motivo})"
        )
        modelo.Add(x[(fix.bombeiro_id, fix.data)] == 1).OnlyEnforceIf(lit)

    # ------------------------------------------------- proibições (contrafactual)
    for bombeiro_id, dia in entrada.proibicoes:
        if (bombeiro_id, dia) in x:
            lit = com_assumption(
                f"hipótese: bombeiro {bombeiro_id} NÃO escalado em {dia.isoformat()}"
            )
            modelo.Add(x[(bombeiro_id, dia)] == 0).OnlyEnforceIf(lit)

    # -------------------------------------------------------- carga esperada
    elegiveis = dias_elegiveis(bombeiros, por_cat, entrada.indisponibilidades)
    esperado = carga_esperada(bombeiros, por_cat, elegiveis)

    return Contexto(
        entrada=entrada,
        modelo=modelo,
        x=x,
        dias=dias,
        bombeiro_ids=[b.id for b in bombeiros],
        mapa_cats=mapa_cats,
        dias_da_categoria=por_cat,
        esperado=esperado,
        assumptions=assumptions,
    )


def diagnosticar_disponibilidade(entrada: EntradaSolve) -> list[str]:
    """Checagem barata, feita antes do solve, para mensagens mais diretas."""
    problemas: list[str] = []
    dias = dias_do_mes(entrada.ano, entrada.mes)
    bombeiros = entrada.bombeiros_ativos()

    for d in dias:
        disponiveis = [
            b
            for b in bombeiros
            if esta_disponivel(b.id, d, entrada.indisponibilidades)
        ]
        if not disponiveis:
            problemas.append(
                f"Nenhum bombeiro disponível em {d.strftime('%d/%m/%Y')}."
            )

    if entrada.parametros.intervalo_minimo_dias > 0:
        capacidade = len(bombeiros) * (
            len(dias) // (entrada.parametros.intervalo_minimo_dias + 1) + 1
        )
        if capacidade < len(dias):
            problemas.append(
                f"Efetivo insuficiente: {len(bombeiros)} bombeiros não cobrem "
                f"{len(dias)} dias respeitando o descanso mínimo."
            )
    return problemas
