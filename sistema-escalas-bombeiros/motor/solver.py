"""Orquestrador do solve — Parte 2, "Otimização lexicográfica em estágios".

Fluxo:
    1. Diagnóstico barato de disponibilidade.
    2. Solve de viabilidade (sem objetivo) com assumptions.
       Se INFEASIBLE -> conjunto mínimo de conflito, e para aqui.
       NUNCA relaxa uma regra obrigatória para "sempre entregar uma resposta".
    3. Estágios E1..E6 em sequência: otimiza, trava o resultado com epsilon
       como restrição do próximo, segue.
    4. Verificador independente sobre a solução final.
"""

from __future__ import annotations

import time

from ortools.sat.python import cp_model

from .calendario import tipo_do_dia
from .dominio import (
    Conflito,
    EntradaSolve,
    OrigemPlantao,
    Plantao,
    ResultadoEstagio,
    ResultadoSolve,
    StatusSolve,
)
from .equidade import saldo_acumulado
from .estagios import Estagio, contar_preferencias, sequencia_padrao
from .modelo import Contexto, construir, diagnosticar_disponibilidade
from .verificador import exigir_valida


def _novo_solver(entrada: EntradaSolve) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    p = entrada.parametros
    # Determinismo: uma escala precisa ser reproduzível para auditoria.
    solver.parameters.random_seed = p.random_seed
    solver.parameters.num_search_workers = p.num_workers
    solver.parameters.max_time_in_seconds = p.tempo_limite_estagio_s
    return solver


def _extrair(ctx: Contexto, solver: cp_model.CpSolver) -> list[Plantao]:
    feriados = {f.data for f in ctx.entrada.feriados}
    travados = {f.data for f in ctx.entrada.fixados}
    plantoes: list[Plantao] = []
    for dia in ctx.dias:
        for bid in ctx.bombeiro_ids:
            if solver.Value(ctx.x[(bid, dia)]) == 1:
                plantoes.append(
                    Plantao(
                        data=dia,
                        bombeiro_id=bid,
                        tipo=tipo_do_dia(dia, feriados),
                        categorias=ctx.mapa_cats[dia],
                        origem=(
                            OrigemPlantao.MANUAL
                            if dia in travados
                            else OrigemPlantao.SOLVER
                        ),
                        travado=dia in travados,
                    )
                )
                break
    return plantoes


def _conflitos(ctx: Contexto, solver: cp_model.CpSolver) -> list[Conflito]:
    """Conjunto mínimo de restrições em conflito.

    Sem isto, "infactível" é um beco sem saída: o supervisor recebe um erro e
    não sabe o que mudar.
    """
    referencias: list[str] = []
    try:
        for indice in solver.SufficientAssumptionsForInfeasibility():
            for lit, rotulo in ctx.assumptions.items():
                if lit.Index() == indice:
                    referencias.append(rotulo)
                    break
    except Exception:  # noqa: BLE001 - API do solver varia entre versões
        referencias = []

    if not referencias:
        return [
            Conflito(
                descricao=(
                    "Não existe escala possível com as regras obrigatórias atuais. "
                    "O solver não conseguiu isolar o conjunto mínimo de conflito."
                )
            )
        ]

    return [
        Conflito(
            descricao=(
                "Estas condições, em conjunto, tornam a cobertura impossível: "
                + "; ".join(referencias)
                + "."
            ),
            referencias=referencias,
        )
    ]


def resolver(
    entrada: EntradaSolve,
    estagios: list[Estagio] | None = None,
    validar: bool = True,
) -> ResultadoSolve:
    inicio = time.monotonic()
    estagios = estagios if estagios is not None else sequencia_padrao()
    hash_entrada = entrada.hash_entrada()

    problemas = diagnosticar_disponibilidade(entrada)

    ctx = construir(entrada)
    solver = _novo_solver(entrada)

    # ---------------------------------------------------- 1) viabilidade
    status = solver.Solve(ctx.modelo)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        conflitos = _conflitos(ctx, solver)
        for texto in problemas:
            conflitos.insert(0, Conflito(descricao=texto))
        return ResultadoSolve(
            status=StatusSolve.INFACTIVEL,
            conflitos=conflitos,
            tempo_s=time.monotonic() - inicio,
            hash_entrada=hash_entrada,
        )

    # ---------------------------------------------------- 2) estágios
    resultados: list[ResultadoEstagio] = []
    status_final = cp_model.OPTIMAL

    for estagio in estagios:
        objetivo = estagio.construir_objetivo(ctx)
        if isinstance(objetivo, int):  # objetivo trivial: nada a otimizar
            resultados.append(
                ResultadoEstagio(
                    codigo=estagio.codigo,
                    descricao=estagio.descricao,
                    valor=objetivo,
                    valor_legivel=estagio.formatar(ctx, objetivo),
                )
            )
            continue

        ctx.modelo.ClearObjective()
        ctx.modelo.Minimize(objetivo)
        status = solver.Solve(ctx.modelo)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # Um estágio não deve tornar o problema infactível: os anteriores
            # já foram travados com folga. Se acontecer, para e devolve o que
            # já se tem, sem violar nada.
            break
        if status == cp_model.FEASIBLE:
            status_final = cp_model.FEASIBLE

        valor = int(solver.Value(objetivo))
        resultados.append(
            ResultadoEstagio(
                codigo=estagio.codigo,
                descricao=estagio.descricao,
                valor=valor,
                valor_legivel=estagio.formatar(ctx, valor),
            )
        )
        # Trava com epsilon, não no valor exato: travar no ótimo exato engessa
        # todos os estágios seguintes, frequentemente sem ganho real.
        ctx.modelo.Add(objetivo <= valor + estagio.epsilon(ctx))

    ctx.modelo.ClearObjective()
    status = solver.Solve(ctx.modelo)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ResultadoSolve(
            status=StatusSolve.ERRO,
            conflitos=[
                Conflito(descricao="Falha ao recuperar a solução final travada.")
            ],
            tempo_s=time.monotonic() - inicio,
            hash_entrada=hash_entrada,
        )

    plantoes = _extrair(ctx, solver)

    # ---------------------------------------------------- 3) verificação
    if validar:
        exigir_valida(entrada, plantoes)

    saldos = saldo_acumulado(entrada, plantoes, ctx.dias_da_categoria)
    atendidas, total = contar_preferencias(ctx, {p.data: p.bombeiro_id for p in plantoes})

    return ResultadoSolve(
        status=(
            StatusSolve.OTIMO if status_final == cp_model.OPTIMAL else StatusSolve.VIAVEL
        ),
        plantoes=plantoes,
        estagios=resultados,
        saldo_final=saldos,
        preferencias_atendidas=atendidas,
        preferencias_total=total,
        tempo_s=time.monotonic() - inicio,
        hash_entrada=hash_entrada,
    )
