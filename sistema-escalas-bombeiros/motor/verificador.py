"""Verificador independente — Parte 2, "Verificador independente".

Escrito SEM olhar para a formulação do modelo CP-SAT: recebe uma escala pronta
e checa as regras obrigatórias do zero, a partir da especificação.

É defesa em profundidade. Um bug na formulação do modelo não deve virar uma
escala inválida em produção. Toda saída do solver passa por aqui antes de ser
persistida, inclusive em produção.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .calendario import dias_do_mes
from .dominio import EntradaSolve, Plantao


@dataclass
class Violacao:
    regra: str
    descricao: str
    data: date | None = None
    bombeiro_id: int | None = None


class EscalaInvalida(Exception):
    def __init__(self, violacoes: list[Violacao]):
        self.violacoes = violacoes
        super().__init__(
            "Escala reprovada pelo verificador independente: "
            + "; ".join(v.descricao for v in violacoes)
        )


def verificar(entrada: EntradaSolve, plantoes: list[Plantao]) -> list[Violacao]:
    """Devolve a lista de violações. Lista vazia = escala válida."""
    violacoes: list[Violacao] = []
    dias = dias_do_mes(entrada.ano, entrada.mes)
    ids_ativos = {b.id for b in entrada.bombeiros_ativos()}

    por_dia: dict[date, list[int]] = {}
    for p in plantoes:
        por_dia.setdefault(p.data, []).append(p.bombeiro_id)

    # --- H1: exatamente um bombeiro por dia, nenhum dia vazio ---------------
    for d in dias:
        escalados = por_dia.get(d, [])
        if len(escalados) == 0:
            violacoes.append(
                Violacao("H1", f"Dia sem cobertura: {d.strftime('%d/%m/%Y')}", d)
            )
        elif len(escalados) > 1:
            violacoes.append(
                Violacao(
                    "H1",
                    f"Mais de um bombeiro em {d.strftime('%d/%m/%Y')}: {escalados}",
                    d,
                )
            )

    for p in plantoes:
        if p.data not in dias:
            violacoes.append(
                Violacao("H1", f"Plantão fora do mês da escala: {p.data}", p.data)
            )
        if p.bombeiro_id not in ids_ativos:
            violacoes.append(
                Violacao(
                    "H1",
                    f"Bombeiro inexistente ou inativo escalado: {p.bombeiro_id}",
                    p.data,
                    p.bombeiro_id,
                )
            )

    # --- H2: nunca escalar em férias/licença/atestado/afastamento -----------
    for p in plantoes:
        for indisp in entrada.indisponibilidades:
            if indisp.bombeiro_id == p.bombeiro_id and indisp.cobre(p.data):
                violacoes.append(
                    Violacao(
                        "H2",
                        f"Bombeiro {p.bombeiro_id} escalado em "
                        f"{p.data.strftime('%d/%m/%Y')} durante {indisp.tipo.value}",
                        p.data,
                        p.bombeiro_id,
                    )
                )

    # --- H3: descanso mínimo, inclusive na virada do mês --------------------
    folga = entrada.parametros.intervalo_minimo_dias
    if folga > 0:
        trabalhados: dict[int, set[date]] = {}
        for p in plantoes:
            trabalhados.setdefault(p.bombeiro_id, set()).add(p.data)
        for pa in entrada.plantoes_anteriores:
            trabalhados.setdefault(pa.bombeiro_id, set()).add(pa.data)

        liberados = set(entrada.excecoes_descanso)

        for bombeiro_id, datas in trabalhados.items():
            ordenadas = sorted(datas)
            for anterior, seguinte in zip(ordenadas, ordenadas[1:]):
                intervalo = (seguinte - anterior).days
                # Exceção autorizada pelo supervisor cobre o par (Parte 0.5).
                if (bombeiro_id, seguinte) in liberados or (
                    bombeiro_id, anterior
                ) in liberados:
                    continue
                if intervalo <= folga:
                    violacoes.append(
                        Violacao(
                            "H3",
                            f"Bombeiro {bombeiro_id} com apenas {intervalo - 1} dia(s) "
                            f"de folga entre {anterior.strftime('%d/%m')} e "
                            f"{seguinte.strftime('%d/%m')}",
                            seguinte,
                            bombeiro_id,
                        )
                    )

    # --- H4: plantões travados preservados ----------------------------------
    escala = {p.data: p.bombeiro_id for p in plantoes}
    for fix in entrada.fixados:
        if fix.data in dias and escala.get(fix.data) != fix.bombeiro_id:
            violacoes.append(
                Violacao(
                    "H4",
                    f"Plantão travado em {fix.data.strftime('%d/%m/%Y')} foi alterado "
                    f"(esperado bombeiro {fix.bombeiro_id}, "
                    f"encontrado {escala.get(fix.data)})",
                    fix.data,
                    fix.bombeiro_id,
                )
            )

    return violacoes


def exigir_valida(entrada: EntradaSolve, plantoes: list[Plantao]) -> None:
    violacoes = verificar(entrada, plantoes)
    if violacoes:
        raise EscalaInvalida(violacoes)


def validar_alteracao(
    entrada: EntradaSolve,
    escala_atual: dict[date, int],
    data: date,
    novo_bombeiro_id: int,
) -> list[Violacao]:
    """Valida um ajuste manual antes de aplicar (Parte 1, "Ajustes")."""
    return validar_alteracoes(entrada, escala_atual, {data: novo_bombeiro_id})


def validar_alteracoes(
    entrada: EntradaSolve,
    escala_atual: dict[date, int],
    mudancas: dict[date, int],
) -> list[Violacao]:
    """Valida VÁRIAS mudanças aplicadas ao mesmo tempo.

    Uma permuta troca dois dias de uma vez. Validar um por vez daria falso
    negativo e falso positivo: o estado intermediário, com a mesma pessoa nos
    dois dias, não é o que vai valer. As duas mudanças precisam ser avaliadas
    juntas, sobre o resultado final.
    """
    nova = dict(escala_atual)
    nova.update(mudancas)
    plantoes = [
        Plantao(data=d, bombeiro_id=b, tipo="", categorias=())
        for d, b in sorted(nova.items())
    ]
    return verificar(entrada, plantoes)
