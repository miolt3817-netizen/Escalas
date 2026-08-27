"""Suíte do motor — Parte 2, "Testes".

Como a Parte 1 exige regras que "nunca podem ser violadas", o motor é tratado
com rigor de sistema crítico:

  * teste baseado em propriedades (Hypothesis) sobre cenários aleatórios;
  * casos de fronteira explícitos (virada de mês, meses de 28/29/31 dias,
    efetivo mínimo, feriado em fim de semana);
  * caso "sem solução viável";
  * verificador independente sobre toda saída;
  * regressão de equidade em 12 meses seguidos.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from motor.calendario import dias_do_mes, dias_por_categoria, mapa_categorias
from motor.dominio import (
    Bombeiro,
    Categoria,
    EntradaSolve,
    Feriado,
    Indisponibilidade,
    Parametros,
    PlantaoAnterior,
    PlantaoFixado,
    StatusSolve,
    TipoIndisponibilidade,
)
from motor.equidade import amplitude, saldo_acumulado
from motor.solver import resolver
from motor.verificador import verificar

RAPIDO = Parametros(tempo_limite_estagio_s=5.0)


def _entrada(n_bombeiros=8, ano=2026, mes=9, **kwargs):
    kwargs.setdefault("parametros", RAPIDO)
    return EntradaSolve(
        ano=ano,
        mes=mes,
        bombeiros=[Bombeiro(i, f"B{i}") for i in range(1, n_bombeiros + 1)],
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Propriedades — as regras obrigatórias NUNCA podem quebrar
# --------------------------------------------------------------------------- #


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(
    n_bombeiros=st.integers(min_value=4, max_value=12),
    mes=st.integers(min_value=1, max_value=12),
    ano=st.sampled_from([2024, 2025, 2026]),
    blocos=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=4),  # bombeiro (índice)
            st.integers(min_value=1, max_value=25),  # dia inicial
            st.integers(min_value=0, max_value=6),  # duração
        ),
        max_size=6,
    ),
    dias_feriado=st.lists(st.integers(min_value=1, max_value=28), max_size=4),
)
def test_propriedade_regras_obrigatorias_nunca_quebram(
    n_bombeiros, mes, ano, blocos, dias_feriado
):
    """Para QUALQUER combinação de entradas: ou o solver devolve uma escala
    válida, ou devolve infactível com diagnóstico. Nunca uma escala inválida."""
    ultimo = calendar.monthrange(ano, mes)[1]

    indisponibilidades = []
    for idx, inicio, duracao in blocos:
        bombeiro_id = (idx % n_bombeiros) + 1
        d0 = date(ano, mes, min(inicio, ultimo))
        d1 = min(d0 + timedelta(days=duracao), date(ano, mes, ultimo))
        indisponibilidades.append(
            Indisponibilidade(bombeiro_id, d0, d1, TipoIndisponibilidade.FERIAS)
        )

    feriados = [
        Feriado(date(ano, mes, min(d, ultimo))) for d in set(dias_feriado)
    ]

    entrada = _entrada(
        n_bombeiros=n_bombeiros,
        ano=ano,
        mes=mes,
        indisponibilidades=indisponibilidades,
        feriados=feriados,
    )

    resultado = resolver(entrada, validar=False)

    if resultado.viavel:
        violacoes = verificar(entrada, resultado.plantoes)
        assert not violacoes, [v.descricao for v in violacoes]
        assert len(resultado.plantoes) == ultimo
    else:
        assert resultado.status == StatusSolve.INFACTIVEL
        assert resultado.conflitos, "infactível sem diagnóstico"


# --------------------------------------------------------------------------- #
# Fronteiras
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ano,mes,dias",
    [(2026, 2, 28), (2024, 2, 29), (2026, 1, 31), (2026, 4, 30)],
)
def test_fronteira_tamanho_do_mes(ano, mes, dias):
    r = resolver(_entrada(ano=ano, mes=mes))
    assert r.viavel
    assert len(r.plantoes) == dias


def test_fronteira_virada_de_mes():
    """H3 atravessa a virada do mês: quem trabalhou em 31/08 não pode
    trabalhar em 01/09. Testes de um mês isolado não pegam essa violação."""
    entrada = _entrada(
        n_bombeiros=5,
        ano=2026,
        mes=9,
        plantoes_anteriores=[
            PlantaoAnterior(date(2026, 8, 31), 1),
            PlantaoAnterior(date(2026, 8, 30), 2),
        ],
    )
    r = resolver(entrada)
    assert r.viavel
    escala = r.escala_por_dia()
    assert escala[date(2026, 9, 1)] != 1, "plantão consecutivo na virada do mês"
    assert not verificar(entrada, r.plantoes)


def test_fronteira_efetivo_minimo():
    """Com folga de 1 dia, 2 bombeiros conseguem alternar; 1 não consegue."""
    assert resolver(_entrada(n_bombeiros=2)).viavel
    r = resolver(_entrada(n_bombeiros=1))
    assert r.status == StatusSolve.INFACTIVEL
    assert r.conflitos


def test_fronteira_feriado_em_fim_de_semana():
    """Um sábado feriado conta em SABADO, FERIADO e VERMELHA ao mesmo tempo."""
    sabado = date(2026, 9, 5)
    assert sabado.weekday() == 5
    mapa = mapa_categorias(dias_do_mes(2026, 9), [Feriado(sabado)])
    cats = mapa[sabado]
    assert Categoria.SABADO in cats
    assert Categoria.FERIADO in cats
    assert Categoria.VERMELHA in cats
    assert Categoria.BRANCA not in cats


def test_infactivel_nao_devolve_escala():
    """Nunca relaxar uma regra obrigatória para 'sempre entregar uma resposta'."""
    entrada = _entrada(
        n_bombeiros=3,
        indisponibilidades=[
            Indisponibilidade(
                i, date(2026, 9, 10), date(2026, 9, 10), TipoIndisponibilidade.ATESTADO
            )
            for i in (1, 2, 3)
        ],
    )
    r = resolver(entrada)
    assert r.status == StatusSolve.INFACTIVEL
    assert r.plantoes == []
    assert any("10/09/2026" in c.descricao for c in r.conflitos)


def test_plantoes_travados_sao_preservados():
    entrada = _entrada(
        fixados=[
            PlantaoFixado(date(2026, 9, 15), 7, "ajuste manual"),
            PlantaoFixado(date(2026, 9, 20), 3, "troca aprovada"),
        ]
    )
    r = resolver(entrada)
    assert r.viavel
    escala = r.escala_por_dia()
    assert escala[date(2026, 9, 15)] == 7
    assert escala[date(2026, 9, 20)] == 3


def test_ferias_nao_geram_deficit():
    """Correção nº 2: quem tira férias não volta com saldo negativo para ser
    sobrecarregado depois. O saldo é proporcional à disponibilidade."""
    entrada = _entrada(
        n_bombeiros=6,
        indisponibilidades=[
            Indisponibilidade(
                1, date(2026, 9, 1), date(2026, 9, 30), TipoIndisponibilidade.FERIAS
            )
        ],
    )
    r = resolver(entrada)
    assert r.viavel
    saldo_de_ferias = r.saldo_final[1][Categoria.TOTAL]
    assert abs(saldo_de_ferias) < 0.01, (
        f"bombeiro de férias o mês inteiro deveria ter saldo neutro, "
        f"tem {saldo_de_ferias}"
    )


# --------------------------------------------------------------------------- #
# Regressão de equidade — 12 meses seguidos
# --------------------------------------------------------------------------- #


def _simular_ano(n_bombeiros: int, meses: int, com_historico: bool):
    """Encadeia N meses seguidos e devolve (contagem_bruta, saldo_final)."""
    bombeiros = [Bombeiro(i, f"B{i}") for i in range(1, n_bombeiros + 1)]
    saldo: dict[int, dict[Categoria, float]] = {}
    anteriores: list[PlantaoAnterior] = []
    contagem: dict[int, int] = {b.id: 0 for b in bombeiros}

    for mes in range(1, meses + 1):
        entrada = EntradaSolve(
            ano=2026,
            mes=mes,
            bombeiros=bombeiros,
            parametros=RAPIDO,
            saldo_historico=saldo if com_historico else {},
            plantoes_anteriores=anteriores,
        )
        r = resolver(entrada)
        assert r.viavel, f"mês {mes} infactível"
        assert not verificar(entrada, r.plantoes)

        for p in r.plantoes:
            contagem[p.bombeiro_id] += 1
        saldo = r.saldo_final
        ultimos = sorted(r.plantoes, key=lambda p: p.data)[-2:]
        anteriores = [PlantaoAnterior(p.data, p.bombeiro_id) for p in ultimos]

    return contagem, saldo


def test_regressao_equidade_12_meses():
    """Prova de que a compensação histórica funciona (correção nº 1).

    Simula um ano inteiro encadeando o saldo de um mês no seguinte.

    Nota sobre o limite: 365 dias / 7 bombeiros = 52,14. Como as atribuições
    são inteiras, alguém necessariamente faz 53 e alguém faz 52 — amplitude 1
    é o PISO MATEMÁTICO, não uma imperfeição do motor.
    """
    contagem, saldo = _simular_ano(n_bombeiros=7, meses=12, com_historico=True)

    total_dias = sum(contagem.values())
    assert total_dias == 365

    amplitude_bruta = max(contagem.values()) - min(contagem.values())
    assert amplitude_bruta <= 1, (
        f"distribuição não convergiu ao piso inteiro: {sorted(contagem.items())}"
    )
    assert amplitude(saldo, Categoria.TOTAL) <= 1.01
    assert amplitude(saldo, Categoria.VERMELHA) <= 1.51


def test_compensacao_historica_supera_o_controle():
    """Controle: sem encadear o saldo, o desequilíbrio ACUMULA.

    É exatamente o que aconteceria com a especificação v1, em que a
    compensação era o último nível lexicográfico e nunca sobrava grau de
    liberdade para ela agir. Este teste falha se alguém reverter a correção.
    """
    com, _ = _simular_ano(n_bombeiros=7, meses=9, com_historico=True)
    sem, _ = _simular_ano(n_bombeiros=7, meses=9, com_historico=False)

    ampl_com = max(com.values()) - min(com.values())
    ampl_sem = max(sem.values()) - min(sem.values())

    assert ampl_com <= 1, f"com histórico deveria estar no piso: {ampl_com}"
    assert ampl_sem > ampl_com, (
        "o controle sem histórico deveria acumular desequilíbrio "
        f"(com={ampl_com}, sem={ampl_sem})"
    )


def test_saldo_historico_influencia_a_escala():
    """Quem entra o mês com saldo positivo deve receber menos plantões."""
    n = 6
    saldo = {1: {c: 3.0 for c in Categoria}}  # bombeiro 1 muito à frente
    entrada = _entrada(n_bombeiros=n, saldo_historico=saldo)
    r = resolver(entrada)
    assert r.viavel

    contagem: dict[int, int] = {}
    for p in r.plantoes:
        contagem[p.bombeiro_id] = contagem.get(p.bombeiro_id, 0) + 1

    media_outros = sum(v for k, v in contagem.items() if k != 1) / (n - 1)
    assert contagem.get(1, 0) < media_outros, (
        f"bombeiro com saldo +3 recebeu {contagem.get(1, 0)} plantões, "
        f"média dos demais {media_outros:.1f}"
    )


def test_determinismo():
    """Mesma entrada, mesma escala — exigência de auditoria."""
    entrada = _entrada(n_bombeiros=9)
    a = resolver(entrada)
    b = resolver(entrada)
    assert a.escala_por_dia() == b.escala_por_dia()
    assert a.hash_entrada == b.hash_entrada


def test_preferencias_sao_atendidas_quando_possivel():
    from motor.dominio import Preferencia, TipoPreferencia

    entrada = _entrada(
        n_bombeiros=8,
        preferencias=[
            Preferencia(3, TipoPreferencia.EVITA, dia_semana=6),  # domingos
            Preferencia(5, TipoPreferencia.QUER, data=date(2026, 9, 25)),
        ],
    )
    r = resolver(entrada)
    assert r.viavel
    escala = r.escala_por_dia()
    domingos = [d for d in escala if d.weekday() == 6]
    assert all(escala[d] != 3 for d in domingos)
    assert escala[date(2026, 9, 25)] == 5


def test_categorias_somam_corretamente():
    dias = dias_do_mes(2026, 9)
    mapa = mapa_categorias(dias, [Feriado(date(2026, 9, 7))])
    por_cat = dias_por_categoria(mapa)
    assert len(por_cat[Categoria.TOTAL]) == 30
    assert len(por_cat[Categoria.BRANCA]) + len(por_cat[Categoria.VERMELHA]) == 30
    assert len(por_cat[Categoria.FERIADO]) == 1


# --------------------------------------------------------------------------- #
# Modo exceção — Parte 0.5
# --------------------------------------------------------------------------- #


def _cenario_sem_saida():
    """Dois de três bombeiros de férias na mesma semana.

    Sobra uma pessoa para dias seguidos, e a regra de descanso torna a
    cobertura impossível. É o caso que a Parte 0.5 antecipa.
    """
    bombeiros = [Bombeiro(i, n) for i, n in [(1, "Ana"), (2, "Bruno"), (3, "Carla")]]
    ausencias = [
        Indisponibilidade(
            2, date(2027, 6, 10), date(2027, 6, 14), TipoIndisponibilidade.FERIAS
        ),
        Indisponibilidade(
            3, date(2027, 6, 10), date(2027, 6, 14), TipoIndisponibilidade.FERIAS
        ),
    ]
    return bombeiros, ausencias


def test_sem_excecao_o_sistema_admite_que_nao_ha_solucao():
    """Não inventa escala inválida: avisa e explica o conflito."""
    bombeiros, ausencias = _cenario_sem_saida()
    entrada = EntradaSolve(
        ano=2027, mes=6, bombeiros=bombeiros, parametros=RAPIDO,
        indisponibilidades=ausencias,
    )
    resultado = resolver(entrada)

    assert resultado.status == StatusSolve.INFACTIVEL
    assert resultado.plantoes == []
    assert resultado.conflitos, "infactível sem diagnóstico"


def test_excecao_autorizada_destrava_a_escala():
    """Com a liberação explícita, a escala sai — e só ali a regra cede."""
    bombeiros, ausencias = _cenario_sem_saida()
    liberados = [(1, date(2027, 6, dia)) for dia in (11, 12, 13, 14)]

    entrada = EntradaSolve(
        ano=2027, mes=6, bombeiros=bombeiros, parametros=RAPIDO,
        indisponibilidades=ausencias, excecoes_descanso=liberados,
    )
    resultado = resolver(entrada)

    assert resultado.viavel, "a exceção deveria tornar a escala possível"
    assert len(resultado.plantoes) == 30

    escala = resultado.escala_por_dia()
    dias = sorted(escala)
    consecutivos = [
        (a, b) for a, b in zip(dias, dias[1:]) if escala[a] == escala[b]
    ]
    autorizados = set(liberados)
    indevidos = [
        (a, b) for a, b in consecutivos
        if (escala[b], b) not in autorizados and (escala[a], a) not in autorizados
    ]
    assert not indevidos, f"plantão consecutivo fora do autorizado: {indevidos}"


def test_excecao_nao_vale_para_outro_bombeiro_nem_outro_dia():
    """A liberação é pontual: um par bombeiro/dia, e mais nada."""
    bombeiros, ausencias = _cenario_sem_saida()
    # autoriza a pessoa errada — continua sem solução
    entrada = EntradaSolve(
        ano=2027, mes=6, bombeiros=bombeiros, parametros=RAPIDO,
        indisponibilidades=ausencias,
        excecoes_descanso=[(2, date(2027, 6, 11))],
    )
    assert resolver(entrada).status == StatusSolve.INFACTIVEL

    # autoriza a pessoa certa no dia errado — idem
    entrada.excecoes_descanso = [(1, date(2027, 6, 25))]
    assert resolver(entrada).status == StatusSolve.INFACTIVEL


def test_verificador_aceita_a_excecao_que_o_solver_usou():
    """Verificador e solver precisam concordar sobre o que foi liberado.

    Se o verificador ignorasse a exceção, ele reprovaria a própria escala que
    o solver acabou de produzir, e nada seria gravado.
    """
    bombeiros, ausencias = _cenario_sem_saida()
    liberados = [(1, date(2027, 6, dia)) for dia in (11, 12, 13, 14)]
    entrada = EntradaSolve(
        ano=2027, mes=6, bombeiros=bombeiros, parametros=RAPIDO,
        indisponibilidades=ausencias, excecoes_descanso=liberados,
    )
    resultado = resolver(entrada)  # validar=True por padrão
    assert resultado.viavel
    assert not verificar(entrada, resultado.plantoes)

    # sem a autorização, o mesmo conjunto de plantões é reprovado
    entrada_sem = EntradaSolve(
        ano=2027, mes=6, bombeiros=bombeiros, parametros=RAPIDO,
        indisponibilidades=ausencias,
    )
    assert verificar(entrada_sem, resultado.plantoes), (
        "sem exceção, o verificador deveria reprovar"
    )
