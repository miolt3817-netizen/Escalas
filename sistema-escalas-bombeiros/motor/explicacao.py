"""Explicabilidade — Parte 2, "Explicabilidade".

A abordagem de "ler o rastro de candidatos eliminados do solver" NÃO é
implementável com CP-SAT: ele é busca com aprendizado de cláusulas e não
produz um log sequencial de eliminação. As duas técnicas que funcionam:

1. Explicar uma ESCOLHA -> re-solve contrafactual.
   Para justificar "João no dia 12", roda de novo com x[João][12] = 0 e
   compara o resultado. A comparação É a explicação.

2. Explicar INFACTIBILIDADE -> conjunto mínimo de conflito via assumptions
   (implementado em solver._conflitos).

Todos os números saem do solver. A camada de IA generativa, se usada, apenas
REDIGE — nunca calcula. O JSON de fatos é persistido para que a explicação
possa ser reconstruída sem depender do modelo de linguagem.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import date

from .dominio import Categoria, EntradaSolve, ResultadoSolve, StatusSolve
from .equidade import amplitude
from .solver import resolver


@dataclass
class FatosExplicacao:
    """Fatos verificáveis. Entrada da camada de redação."""

    tipo: str
    data: str | None = None
    bombeiro_id: int | None = None
    veredito: str = ""
    estagio_afetado: str | None = None
    valor_original: int | None = None
    valor_contrafactual: int | None = None
    detalhes: dict = field(default_factory=dict)

    def json(self) -> dict:
        return asdict(self)


def explicar_escolha(
    entrada: EntradaSolve, resultado: ResultadoSolve, dia: date
) -> tuple[FatosExplicacao, str]:
    """Por que este bombeiro, neste dia? Re-solve contrafactual.

    Custa um solve extra — irrelevante quando cada solve leva segundos.
    Rodar sob demanda (o supervisor clica no dia), nunca para os 30 dias.
    """
    escala = resultado.escala_por_dia()
    escolhido = escala.get(dia)
    if escolhido is None:
        fatos = FatosExplicacao(
            tipo="escolha", data=dia.isoformat(), veredito="sem_plantao"
        )
        return fatos, "Não há plantão registrado nesta data."

    nomes = {b.id: b.nome for b in entrada.bombeiros}
    nome = nomes.get(escolhido, f"Bombeiro {escolhido}")

    hipotese = copy.deepcopy(entrada)
    hipotese.proibicoes = list(entrada.proibicoes) + [(escolhido, dia)]
    alternativa = resolver(hipotese, validar=False)

    # --- caso 1: sem ele, não há solução -----------------------------------
    if not alternativa.viavel:
        motivos = "; ".join(c.descricao for c in alternativa.conflitos)
        fatos = FatosExplicacao(
            tipo="escolha",
            data=dia.isoformat(),
            bombeiro_id=escolhido,
            veredito="unica_opcao",
            detalhes={"conflitos": motivos},
        )
        texto = (
            f"{nome} foi escolhido para {dia.strftime('%d/%m/%Y')} porque era a única "
            f"opção que respeitava todas as restrições obrigatórias."
        )
        return fatos, texto

    # --- caso 2: sem ele, piora em algum estágio ---------------------------
    originais = {e.codigo: e for e in resultado.estagios}
    for est in alternativa.estagios:
        base = originais.get(est.codigo)
        if base is None:
            continue
        if est.valor > base.valor:
            fatos = FatosExplicacao(
                tipo="escolha",
                data=dia.isoformat(),
                bombeiro_id=escolhido,
                veredito="melhor_opcao",
                estagio_afetado=est.codigo,
                valor_original=base.valor,
                valor_contrafactual=est.valor,
                detalhes={"criterio": base.descricao},
            )
            texto = (
                f"{nome} foi escolhido para {dia.strftime('%d/%m/%Y')} porque qualquer "
                f"outra escolha pioraria o critério \"{base.descricao.lower()}\" "
                f"(de {base.valor_legivel} para {est.valor_legivel})."
            )
            return fatos, texto

    # --- caso 3: havia alternativas equivalentes ---------------------------
    fatos = FatosExplicacao(
        tipo="escolha",
        data=dia.isoformat(),
        bombeiro_id=escolhido,
        veredito="equivalente",
    )
    texto = (
        f"Havia alternativas igualmente boas para {dia.strftime('%d/%m/%Y')}. "
        f"{nome} foi escolhido por desempate, sem prejuízo a nenhum critério."
    )
    return fatos, texto


def sugerir_substitutos(
    entrada: EntradaSolve, resultado: ResultadoSolve, dia: date, limite: int = 3
) -> list[dict]:
    """Parte 1, "Imprevistos": melhores substitutos para um dia específico.

    Ordena por quem menos prejudica o equilíbrio, testando cada candidato como
    plantão fixado e comparando os estágios.
    """
    from .dominio import PlantaoFixado

    escala = resultado.escala_por_dia()
    atual = escala.get(dia)
    nomes = {b.id: b.nome for b in entrada.bombeiros}
    candidatos: list[dict] = []

    for b in entrada.bombeiros_ativos():
        if b.id == atual:
            continue
        hipotese = copy.deepcopy(entrada)
        hipotese.fixados = [
            f for f in entrada.fixados if f.data != dia
        ] + [PlantaoFixado(data=dia, bombeiro_id=b.id, motivo="substituição")]
        if atual is not None:
            hipotese.proibicoes = list(entrada.proibicoes) + [(atual, dia)]
        alt = resolver(hipotese, validar=False)
        if not alt.viavel:
            continue
        custo = tuple(e.valor for e in alt.estagios)
        candidatos.append(
            {
                "bombeiro_id": b.id,
                "nome": nomes.get(b.id, f"Bombeiro {b.id}"),
                "custo": custo,
                "estagios": [
                    {"codigo": e.codigo, "valor_legivel": e.valor_legivel}
                    for e in alt.estagios
                ],
            }
        )

    candidatos.sort(key=lambda c: c["custo"])
    for c in candidatos:
        c.pop("custo")
    return candidatos[:limite]


def resumo_da_escala(
    entrada: EntradaSolve, resultado: ResultadoSolve
) -> tuple[dict, str]:
    """Parte 1, "Assistente inteligente". Todos os números vêm do solver."""
    if not resultado.viavel:
        fatos = {
            "gerada": False,
            "conflitos": [c.descricao for c in resultado.conflitos],
        }
        texto = (
            "Não foi possível gerar a escala sem violar uma regra obrigatória. "
            + " ".join(c.descricao for c in resultado.conflitos)
        )
        return fatos, texto

    dias_cobertos = len({p.data for p in resultado.plantoes})
    consecutivos = next(
        (e for e in resultado.estagios if e.codigo == "E1"), None
    )
    ampl_vermelha = amplitude(resultado.saldo_final, Categoria.VERMELHA)
    ampl_total = amplitude(resultado.saldo_final, Categoria.TOTAL)

    fatos = {
        "gerada": True,
        "dias_cobertos": dias_cobertos,
        "sem_consecutivos": True,  # garantido por H3 + verificador
        "pares_proximos": consecutivos.valor if consecutivos else 0,
        "preferencias_atendidas": resultado.preferencias_atendidas,
        "preferencias_total": resultado.preferencias_total,
        "amplitude_total": round(ampl_total, 2),
        "amplitude_vermelha": round(ampl_vermelha, 2),
        "tempo_s": round(resultado.tempo_s, 2),
        "hash_entrada": resultado.hash_entrada,
    }

    partes = [
        "A escala foi gerada com sucesso.",
        f"Todos os {dias_cobertos} dias possuem cobertura.",
        "Nenhum bombeiro ficou em plantões consecutivos.",
    ]
    if resultado.preferencias_total:
        partes.append(
            f"{resultado.preferencias_atendidas} de "
            f"{resultado.preferencias_total} preferências foram atendidas."
        )
    partes.append(
        f"A diferença de carga entre o mais e o menos escalado é de "
        f"{ampl_total:.2f} plantão, e a de fins de semana e feriados, "
        f"de {ampl_vermelha:.2f}."
    )
    partes.append(
        "As diferenças restantes entram no saldo de equidade e serão "
        "compensadas automaticamente nos próximos meses."
    )
    return fatos, " ".join(partes)
