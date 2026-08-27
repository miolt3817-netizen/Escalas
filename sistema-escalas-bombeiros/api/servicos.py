"""Serviços — ponte entre o banco e o motor.

O motor não conhece banco de dados. Este módulo traduz linhas do Postgres em
`EntradaSolve` e o `ResultadoSolve` de volta em linhas.

O saldo de equidade é SEMPRE derivado de `plantoes` (escalas publicadas),
nunca lido de um contador armazenado. Em produção há uma view materializada
(infra/init.sql) com o mesmo cálculo; aqui a versão em Python é a referência.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from motor import equidade as eq
from motor.calendario import dias_do_mes, dias_por_categoria, mapa_categorias
from motor.dominio import (
    Bombeiro as BombeiroDom,
    Categoria,
    EntradaSolve,
    Feriado as FeriadoDom,
    Indisponibilidade as IndispDom,
    Parametros,
    Plantao as PlantaoDom,
    PlantaoAnterior,
    PlantaoFixado,
    Preferencia as PrefDom,
    ResultadoSolve,
    TipoIndisponibilidade,
    TipoPreferencia,
)
from motor.explicacao import resumo_da_escala
from motor.solver import resolver
from motor.dominio import Categoria
from motor.verificador import validar_alteracao, validar_alteracoes

from . import modelos as m

CAMPOS_PARAMETROS = {
    "duracao_plantao_horas": int,
    "hora_inicio": int,
    "intervalo_minimo_dias": int,
    "intervalo_desejavel_dias": int,
    "criterio_classificacao": str,
    "escala_inteira": int,
    "epsilon_espacamento": int,
    "epsilon_equidade": int,
    "epsilon_preferencias": int,
    "tempo_limite_estagio_s": float,
    "random_seed": int,
    "num_workers": int,
}


# --------------------------------------------------------------------------- #
# Leitura de configuração
# --------------------------------------------------------------------------- #


def carregar_parametros(db: Session) -> Parametros:
    linhas = db.scalars(select(m.Parametro)).all()
    valores = {}
    for linha in linhas:
        conversor = CAMPOS_PARAMETROS.get(linha.chave)
        if conversor is None:
            continue
        try:
            valores[linha.chave] = conversor(linha.valor)
        except (TypeError, ValueError):
            continue
    return Parametros(**valores)


def bombeiros_ativos(db: Session) -> list[BombeiroDom]:
    linhas = db.scalars(
        select(m.Usuario).where(
            and_(m.Usuario.papel == "bombeiro", m.Usuario.ativo.is_(True))
        )
    ).all()
    return [BombeiroDom(id=u.id, nome=u.nome, ativo=True) for u in linhas]


# --------------------------------------------------------------------------- #
# Saldo de equidade — DERIVADO de plantoes, nunca armazenado
# --------------------------------------------------------------------------- #


def saldo_historico(
    db: Session, ate_ano: int, ate_mes: int
) -> dict[int, dict[Categoria, float]]:
    """Saldo acumulado de todas as escalas PUBLICADAS anteriores ao mês alvo.

    Uma escala em rascunho não conta: ela ainda pode ser descartada.
    """
    corte = date(ate_ano, ate_mes, 1)
    bombeiros = bombeiros_ativos(db)
    if not bombeiros:
        return {}

    escalas = db.scalars(
        select(m.Escala).where(m.Escala.status == "publicada")
    ).all()
    escalas = [e for e in escalas if date(e.ano, e.mes, 1) < corte]
    if not escalas:
        return {b.id: {c: 0.0 for c in Categoria} for b in bombeiros}

    feriados = [FeriadoDom(f.data, f.nome, f.ambito) for f in db.scalars(select(m.Feriado)).all()]
    indisp = _indisponibilidades(db)

    acumulado: dict[int, dict[Categoria, float]] = {
        b.id: {c: 0.0 for c in Categoria} for b in bombeiros
    }

    for escala in escalas:
        dias = dias_do_mes(escala.ano, escala.mes)
        mapa = mapa_categorias(dias, feriados)
        por_cat = dias_por_categoria(mapa)

        plantoes = [
            PlantaoDom(
                data=p.data,
                bombeiro_id=p.bombeiro_id,
                tipo=p.tipo,
                categorias=mapa.get(p.data, ()),
            )
            for p in escala.plantoes
        ]
        parcial = eq.saldo_do_periodo(bombeiros, plantoes, por_cat, indisp)
        for bid, cats in parcial.items():
            for cat, valor in cats.items():
                acumulado[bid][cat] += valor

    return acumulado


def _indisponibilidades(db: Session) -> list[IndispDom]:
    return [
        IndispDom(
            bombeiro_id=i.bombeiro_id,
            inicio=i.inicio,
            fim=i.fim,
            tipo=TipoIndisponibilidade(i.tipo),
            id=i.id,
        )
        for i in db.scalars(select(m.Indisponibilidade)).all()
    ]


# --------------------------------------------------------------------------- #
# Montagem da entrada do solve
# --------------------------------------------------------------------------- #


def montar_entrada(
    db: Session,
    ano: int,
    mes: int,
    preservar_travados: bool = True,
    congelar_ate: date | None = None,
) -> EntradaSolve:
    parametros = carregar_parametros(db)
    dias = dias_do_mes(ano, mes)

    feriados = [
        FeriadoDom(f.data, f.nome, f.ambito)
        for f in db.scalars(select(m.Feriado)).all()
        if dias[0] <= f.data <= dias[-1]
    ]

    preferencias = [
        PrefDom(
            bombeiro_id=p.bombeiro_id,
            tipo=TipoPreferencia(p.tipo),
            data=p.data,
            dia_semana=p.dia_semana,
            peso=p.peso,
        )
        for p in db.scalars(select(m.Preferencia)).all()
    ]

    # Continuidade entre meses: os últimos dias do mês anterior entram como
    # dados fixos. Sem isto, o sistema produz plantão consecutivo em 31/08→01/09.
    folga = parametros.intervalo_minimo_dias
    anteriores: list[PlantaoAnterior] = []
    if folga > 0:
        inicio_janela = dias[0] - timedelta(days=folga)
        publicadas = db.scalars(
            select(m.Escala).where(m.Escala.status == "publicada")
        ).all()
        for escala in publicadas:
            for p in escala.plantoes:
                if inicio_janela <= p.data < dias[0]:
                    anteriores.append(PlantaoAnterior(p.data, p.bombeiro_id))

    # Regeneração parcial: dias já decorridos e plantões travados ficam fixos.
    fixados: list[PlantaoFixado] = []
    atual = escala_vigente(db, ano, mes)
    if atual is not None:
        for p in atual.plantoes:
            travar = (preservar_travados and p.travado) or (
                congelar_ate is not None and p.data <= congelar_ate
            )
            if travar:
                motivo = "decorrido" if (
                    congelar_ate and p.data <= congelar_ate
                ) else p.origem
                fixados.append(PlantaoFixado(p.data, p.bombeiro_id, motivo))

    return EntradaSolve(
        ano=ano,
        mes=mes,
        bombeiros=bombeiros_ativos(db),
        parametros=parametros,
        indisponibilidades=_indisponibilidades(db),
        feriados=feriados,
        preferencias=preferencias,
        fixados=fixados,
        plantoes_anteriores=anteriores,
        saldo_historico=saldo_historico(db, ano, mes),
        excecoes_descanso=[
            (e.bombeiro_id, e.data)
            for e in db.scalars(
                select(m.Excecao).where(m.Excecao.regra_dispensada == "H3")
            ).all()
            if dias[0] <= e.data <= dias[-1]
        ],
    )


# --------------------------------------------------------------------------- #
# Escalas e versionamento
# --------------------------------------------------------------------------- #


def escala_vigente(
    db: Session, ano: int, mes: int, incluir_rascunho: bool = True
) -> m.Escala | None:
    """Publicada se houver; senão, o rascunho de maior versão.

    `incluir_rascunho=False` é o que o bombeiro enxerga. Rascunho é escala que
    o supervisor ainda não aprovou: pode mudar inteira, ou ser descartada. Se
    o bombeiro a visse, planejaria a vida em cima de um plantão que talvez
    nunca exista — e a publicação, exigida na Parte 1, perderia o sentido.
    """
    publicada = db.scalars(
        select(m.Escala).where(
            and_(m.Escala.ano == ano, m.Escala.mes == mes, m.Escala.status == "publicada")
        )
    ).first()
    if not incluir_rascunho:
        return publicada

    rascunho = db.scalars(
        select(m.Escala)
        .where(
            and_(m.Escala.ano == ano, m.Escala.mes == mes, m.Escala.status == "rascunho")
        )
        .order_by(m.Escala.versao.desc())
    ).first()

    if rascunho is None:
        return publicada
    if publicada is None:
        return rascunho
    # Rascunho mais novo que a publicada significa que o supervisor acabou de
    # regerar e ainda não decidiu. Ele precisa revisar antes de publicar — se
    # a publicada tivesse prioridade sempre, gerar de novo não mudaria nada na
    # tela e o fluxo revisar → publicar ficaria quebrado.
    return rascunho if rascunho.versao > publicada.versao else publicada


def proxima_versao(db: Session, ano: int, mes: int) -> int:
    existentes = db.scalars(
        select(m.Escala.versao).where(
            and_(m.Escala.ano == ano, m.Escala.mes == mes)
        )
    ).all()
    return (max(existentes) + 1) if existentes else 1


def gerar_escala(
    db: Session, ano: int, mes: int, usuario_id: int | None = None
) -> tuple[m.Escala | None, ResultadoSolve, str]:
    """Gera uma nova VERSÃO de escala. Nunca sobrescreve uma publicada."""
    entrada = montar_entrada(db, ano, mes)
    resultado = resolver(entrada)

    if not resultado.viavel:
        return None, resultado, ""

    # Duas gerações do mesmo mês em paralelo leem o mesmo max(versao) e
    # disputam o mesmo número, quebrando na restrição de unicidade. Tentar de
    # novo com o número recalculado resolve sem travar tabela.
    escala = None
    for tentativa in range(5):
        candidata = m.Escala(
            ano=ano,
            mes=mes,
            versao=proxima_versao(db, ano, mes),
            status="rascunho",
            criada_por=usuario_id,
        )
        db.add(candidata)
        try:
            db.flush()
            escala = candidata
            break
        except IntegrityError:
            db.rollback()
            if tentativa == 4:
                raise

    for p in resultado.plantoes:
        db.add(
            m.Plantao(
                escala_id=escala.id,
                data=p.data,
                bombeiro_id=p.bombeiro_id,
                tipo=p.tipo,
                origem=p.origem.value,
                travado=p.travado,
            )
        )

    fatos, texto = resumo_da_escala(entrada, resultado)
    db.add(
        m.Explicacao(
            escala_id=escala.id, escopo="mes", fatos_json=fatos, texto=texto
        )
    )
    db.add(
        m.SolveSnapshot(
            escala_id=escala.id,
            hash_entrada=resultado.hash_entrada,
            entrada_json={
                "ano": ano,
                "mes": mes,
                "bombeiros": [b.id for b in entrada.bombeiros],
                "indisponibilidades": [
                    {
                        "bombeiro_id": i.bombeiro_id,
                        "inicio": i.inicio.isoformat(),
                        "fim": i.fim.isoformat(),
                        "tipo": i.tipo.value,
                    }
                    for i in entrada.indisponibilidades
                ],
                "feriados": [f.data.isoformat() for f in entrada.feriados],
                "saldo_historico": {
                    str(bid): {c.value: round(v, 4) for c, v in cats.items()}
                    for bid, cats in entrada.saldo_historico.items()
                },
                "parametros": entrada.parametros.__dict__,
            },
            estagios_json=[
                {
                    "codigo": e.codigo,
                    "descricao": e.descricao,
                    "valor": e.valor,
                    "legivel": e.valor_legivel,
                }
                for e in resultado.estagios
            ],
            tempo_s=resultado.tempo_s,
        )
    )
    db.commit()
    return escala, resultado, texto


def publicar(db: Session, escala_id: int) -> m.Escala:
    escala = db.get(m.Escala, escala_id)
    if escala is None:
        raise ValueError("Escala não encontrada.")
    if escala.status == "publicada":
        return escala

    anteriores = db.scalars(
        select(m.Escala).where(
            and_(
                m.Escala.ano == escala.ano,
                m.Escala.mes == escala.mes,
                m.Escala.status == "publicada",
            )
        )
    ).all()
    for antiga in anteriores:
        antiga.status = "substituida"

    escala.status = "publicada"
    escala.publicada_em = datetime.now(UTC)
    db.commit()
    return escala


# --------------------------------------------------------------------------- #
# Ajustes e trocas — sempre validados
# --------------------------------------------------------------------------- #


def ajustar_plantao(
    db: Session, plantao_id: int, novo_bombeiro_id: int, origem: str = "manual"
) -> tuple[bool, list[str]]:
    """Valida contra as regras obrigatórias ANTES de aplicar."""
    plantao = db.get(m.Plantao, plantao_id)
    if plantao is None:
        return False, ["Plantão não encontrado."]

    escala = db.get(m.Escala, plantao.escala_id)
    entrada = montar_entrada(db, escala.ano, escala.mes, preservar_travados=False)
    atual = {p.data: p.bombeiro_id for p in escala.plantoes}

    violacoes = validar_alteracao(entrada, atual, plantao.data, novo_bombeiro_id)
    if violacoes:
        return False, _com_nomes(db, [v.descricao for v in violacoes])

    plantao.bombeiro_id = novo_bombeiro_id
    plantao.origem = origem
    plantao.travado = True
    db.commit()
    return True, []


# --------------------------------------------------------------------------- #
# Trocas — Parte 1, "Trocas"
# --------------------------------------------------------------------------- #


def _com_nomes(db: Session, descricoes: list[str]) -> list[str]:
    """Troca "Bombeiro 9" pelo nome da pessoa.

    O verificador do motor não conhece o banco, então fala em identificadores.
    Quem lê a mensagem é o supervisor, que precisa do nome para agir.
    """
    import re

    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}

    def substituir(achado: re.Match) -> str:
        return nomes.get(int(achado.group(1)), achado.group(0))

    return [re.sub(r"[Bb]ombeiro (\d+)", substituir, d) for d in descricoes]


def _escala_da_troca(db: Session, troca: m.Troca) -> m.Escala | None:
    plantao = db.get(m.Plantao, troca.plantao_id)
    return db.get(m.Escala, plantao.escala_id) if plantao else None


def mudancas_da_troca(db: Session, troca: m.Troca) -> dict[date, int]:
    """O que a troca altera na escala, em datas e bombeiros.

    Cessão muda um dia; permuta muda dois. É este conjunto que precisa ser
    validado de uma vez só.
    """
    origem = db.get(m.Plantao, troca.plantao_id)
    if origem is None or troca.aceitante_id is None:
        return {}

    mudancas = {origem.data: troca.aceitante_id}
    if troca.plantao_oferecido_id:
        oferecido = db.get(m.Plantao, troca.plantao_oferecido_id)
        if oferecido is not None:
            mudancas[oferecido.data] = troca.solicitante_id
    return mudancas


def validar_troca(db: Session, troca: m.Troca) -> list[str]:
    """Checa a troca contra as regras obrigatórias. Lista vazia = pode aplicar.

    Roda no momento da APROVAÇÃO, não no do pedido: entre um e outro a escala
    pode ter mudado — outro ajuste, outra troca aprovada, uma indisponibilidade
    nova. Validar só na criação deixaria passar troca que virou inválida.
    """
    escala = _escala_da_troca(db, troca)
    if escala is None:
        return ["A escala deste plantão não existe mais."]
    if escala.status != "publicada":
        return ["Só é possível trocar plantões de uma escala publicada."]

    mudancas = mudancas_da_troca(db, troca)
    if not mudancas:
        return ["A troca não tem um aceitante definido."]

    ativos = {b.id for b in bombeiros_ativos(db)}
    for bombeiro_id in mudancas.values():
        if bombeiro_id not in ativos:
            return ["Um dos envolvidos não está mais ativo."]

    entrada = montar_entrada(
        db, escala.ano, escala.mes, preservar_travados=False
    )
    atual = {p.data: p.bombeiro_id for p in escala.plantoes}
    violacoes = validar_alteracoes(entrada, atual, mudancas)
    return _com_nomes(db, [v.descricao for v in violacoes])


def aplicar_troca(db: Session, troca: m.Troca) -> tuple[bool, list[str]]:
    """Valida e aplica. Nada é alterado se a validação reprovar."""
    problemas = validar_troca(db, troca)
    if problemas:
        return False, problemas

    escala = _escala_da_troca(db, troca)
    por_data = {p.data: p for p in escala.plantoes}
    for data_alvo, bombeiro_id in mudancas_da_troca(db, troca).items():
        plantao = por_data.get(data_alvo)
        if plantao is None:
            return False, [f"O plantão de {data_alvo:%d/%m/%Y} não existe mais."]
        plantao.bombeiro_id = bombeiro_id
        plantao.origem = "troca"
        plantao.travado = True
    return True, []


# --------------------------------------------------------------------------- #
# Candidatos para um dia — Parte 1, "Imprevistos"
# --------------------------------------------------------------------------- #


def candidatos_para(db: Session, escala: m.Escala, alvo: date) -> list[dict]:
    """Quem poderia assumir este dia, e por que sim ou por que não.

    Ordena por saldo de equidade: quem trabalhou menos aparece primeiro, que é
    a mesma prioridade do algoritmo. Quem está impedido vai para o fim, com o
    motivo em texto — o supervisor precisa saber POR QUE não pode, não só que
    não pode.
    """
    atual = {p.data: p.bombeiro_id for p in escala.plantoes}
    escalado_hoje = atual.get(alvo)
    entrada = montar_entrada(db, escala.ano, escala.mes, preservar_travados=False)
    saldos = saldo_historico(db, escala.ano, escala.mes)
    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}

    preferencias = db.scalars(select(m.Preferencia)).all()
    indisponibilidades = db.scalars(select(m.Indisponibilidade)).all()

    saida = []
    for bombeiro in bombeiros_ativos(db):
        if bombeiro.id == escalado_hoje:
            continue

        violacoes = validar_alteracoes(entrada, atual, {alvo: bombeiro.id})
        elegivel = not violacoes

        # A mensagem do verificador é técnica demais para a tela; traduz.
        bloqueio = ""
        if violacoes:
            ferias = next(
                (
                    i for i in indisponibilidades
                    if i.bombeiro_id == bombeiro.id and i.inicio <= alvo <= i.fim
                ),
                None,
            )
            if ferias:
                artigo = "de" if ferias.tipo == "ferias" else "em"
                bloqueio = f"está {artigo} {ferias.tipo} nesta data"
            else:
                vizinho = next(
                    (
                        d for d in (alvo - timedelta(days=1), alvo + timedelta(days=1))
                        if atual.get(d) == bombeiro.id
                    ),
                    None,
                )
                bloqueio = (
                    f"trabalha em {vizinho:%d/%m}, sem o descanso mínimo"
                    if vizinho
                    else _com_nomes(db, [violacoes[0].descricao])[0]
                )

        saldo = saldos.get(bombeiro.id, {}).get(Categoria.TOTAL, 0.0)
        if saldo <= -0.5:
            nota = "está atrás na conta de plantões"
        elif saldo >= 0.5:
            nota = "já está à frente na conta"
        else:
            nota = "está em dia com a média"

        preferencia = next(
            (
                p.tipo for p in preferencias
                if p.bombeiro_id == bombeiro.id
                and (p.data == alvo or p.dia_semana == alvo.weekday())
            ),
            None,
        )

        saida.append(
            {
                "bombeiro_id": bombeiro.id,
                "nome": nomes.get(bombeiro.id, "?"),
                "elegivel": elegivel,
                "bloqueio": bloqueio,
                "nota": nota,
                "saldo_total": round(saldo, 2),
                "preferencia": preferencia,
            }
        )

    # Impedidos por último; entre os livres, quem tem menos plantões primeiro.
    saida.sort(key=lambda c: (not c["elegivel"], c["saldo_total"], c["nome"]))
    return saida
