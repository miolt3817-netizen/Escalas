"""Tipos de domínio do motor de escalas.

Este módulo não conhece banco de dados nem API. Recebe dados puros e devolve
dados puros — o que permite testar o motor isoladamente (Parte 2, "Testes").
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Categoria(str, Enum):
    """Categorias contabilizadas no saldo de equidade (Parte 1)."""

    TOTAL = "total"
    BRANCA = "branca"
    VERMELHA = "vermelha"
    SABADO = "sabado"
    DOMINGO = "domingo"
    FERIADO = "feriado"


#: Ordem dos estágios de equalização (E2..E5). Ver Parte 0.7.
CATEGORIAS_EQUALIZADAS: tuple[Categoria, ...] = (
    Categoria.TOTAL,
    Categoria.BRANCA,
    Categoria.VERMELHA,
    Categoria.SABADO,
    Categoria.DOMINGO,
    Categoria.FERIADO,
)


class TipoIndisponibilidade(str, Enum):
    FERIAS = "ferias"
    LICENCA = "licenca"
    ATESTADO = "atestado"
    AFASTAMENTO = "afastamento"


class TipoPreferencia(str, Enum):
    QUER = "quer"
    EVITA = "evita"


class OrigemPlantao(str, Enum):
    SOLVER = "solver"
    MANUAL = "manual"
    TROCA = "troca"


class StatusSolve(str, Enum):
    OTIMO = "otimo"
    VIAVEL = "viavel"
    INFACTIVEL = "infactivel"
    ERRO = "erro"


# --------------------------------------------------------------------------- #
# Entradas
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Parametros:
    """Valores da Parte 0 da especificação. Configuráveis, nunca hardcoded."""

    #: 0.1 — duração do plantão em horas.
    duracao_plantao_horas: int = 24
    #: 0.1 — hora de início do plantão.
    hora_inicio: int = 8
    #: 0.2 — dias de folga obrigatórios entre plantões. 1 = sem dias consecutivos.
    intervalo_minimo_dias: int = 1
    #: 0.4 — intervalo desejável (soft, estágio E1).
    intervalo_desejavel_dias: int = 3
    #: 0.3 — "inicio" classifica pelo dia em que o plantão começa.
    criterio_classificacao: str = "inicio"
    #: 0.6 — escala de conversão float -> inteiro para o CP-SAT.
    escala_inteira: int = 100
    #: Folga permitida ao travar o ótimo de cada estágio (Parte 2, epsilon).
    epsilon_espacamento: int = 0
    epsilon_equidade: int = 50  # 0,5 plantão
    epsilon_preferencias: int = 0
    #: Limite de tempo por estágio, em segundos.
    tempo_limite_estagio_s: float = 10.0
    #: Determinismo (Parte 2, "Determinismo e reprodutibilidade").
    random_seed: int = 0
    num_workers: int = 1

    def validar(self) -> None:
        if self.intervalo_minimo_dias < 0:
            raise ValueError("intervalo_minimo_dias não pode ser negativo")
        if self.intervalo_desejavel_dias < self.intervalo_minimo_dias:
            raise ValueError(
                "intervalo_desejavel_dias deve ser >= intervalo_minimo_dias"
            )
        if self.escala_inteira <= 0:
            raise ValueError("escala_inteira deve ser positiva")
        if self.criterio_classificacao not in ("inicio",):
            raise ValueError(
                f"criterio_classificacao desconhecido: {self.criterio_classificacao}"
            )


@dataclass(frozen=True)
class Bombeiro:
    id: int
    nome: str
    ativo: bool = True


@dataclass(frozen=True)
class Indisponibilidade:
    bombeiro_id: int
    inicio: date
    fim: date  # inclusivo
    tipo: TipoIndisponibilidade = TipoIndisponibilidade.FERIAS
    id: int | None = None

    def cobre(self, dia: date) -> bool:
        return self.inicio <= dia <= self.fim


@dataclass(frozen=True)
class Feriado:
    data: date
    nome: str = ""
    ambito: str = "nacional"


@dataclass(frozen=True)
class Preferencia:
    bombeiro_id: int
    tipo: TipoPreferencia
    #: Data específica, ou None se for por dia da semana.
    data: date | None = None
    #: 0 = segunda ... 6 = domingo. Usado quando `data` é None.
    dia_semana: int | None = None
    peso: int = 1

    def alvos(self, dias: list[date]) -> list[date]:
        if self.data is not None:
            return [self.data] if self.data in dias else []
        if self.dia_semana is not None:
            return [d for d in dias if d.weekday() == self.dia_semana]
        return []


@dataclass(frozen=True)
class PlantaoFixado:
    """Dia travado: já decorrido, ajustado manualmente ou resultado de troca."""

    data: date
    bombeiro_id: int
    motivo: str = "travado"


@dataclass(frozen=True)
class PlantaoAnterior:
    """Plantão do fim do mês anterior — semeia a continuidade entre meses."""

    data: date
    bombeiro_id: int


@dataclass
class EntradaSolve:
    """Tudo que o motor precisa para gerar uma escala."""

    ano: int
    mes: int
    bombeiros: list[Bombeiro]
    parametros: Parametros = field(default_factory=Parametros)
    indisponibilidades: list[Indisponibilidade] = field(default_factory=list)
    feriados: list[Feriado] = field(default_factory=list)
    preferencias: list[Preferencia] = field(default_factory=list)
    fixados: list[PlantaoFixado] = field(default_factory=list)
    plantoes_anteriores: list[PlantaoAnterior] = field(default_factory=list)
    #: saldo_historico[bombeiro_id][categoria] — Parte 1, "Índice de equidade".
    saldo_historico: dict[int, dict[Categoria, float]] = field(default_factory=dict)
    #: Proibições extras usadas pelo contrafactual da explicabilidade.
    proibicoes: list[tuple[int, date]] = field(default_factory=list)
    #: Exceções autorizadas pelo supervisor (Parte 0.5): pares (bombeiro, dia)
    #: liberados da regra de descanso. O sistema NUNCA cria estas sozinho —
    #: cada uma exige autorização explícita e justificativa registrada.
    excecoes_descanso: list[tuple[int, date]] = field(default_factory=list)

    def bombeiros_ativos(self) -> list[Bombeiro]:
        return [b for b in self.bombeiros if b.ativo]

    def saldo_de(self, bombeiro_id: int, categoria: Categoria) -> float:
        return self.saldo_historico.get(bombeiro_id, {}).get(categoria, 0.0)

    def hash_entrada(self) -> str:
        """Snapshot reprodutível das entradas (Parte 2, `solve_snapshots`)."""
        payload = {
            "ano": self.ano,
            "mes": self.mes,
            "bombeiros": sorted(
                (b.id, b.nome, b.ativo) for b in self.bombeiros
            ),
            "parametros": sorted(self.parametros.__dict__.items()),
            "indisponibilidades": sorted(
                (i.bombeiro_id, i.inicio.isoformat(), i.fim.isoformat(), i.tipo.value)
                for i in self.indisponibilidades
            ),
            "feriados": sorted(f.data.isoformat() for f in self.feriados),
            "preferencias": sorted(
                (
                    p.bombeiro_id,
                    p.tipo.value,
                    p.data.isoformat() if p.data else "",
                    p.dia_semana if p.dia_semana is not None else -1,
                    p.peso,
                )
                for p in self.preferencias
            ),
            "fixados": sorted(
                (f.data.isoformat(), f.bombeiro_id) for f in self.fixados
            ),
            "anteriores": sorted(
                (p.data.isoformat(), p.bombeiro_id) for p in self.plantoes_anteriores
            ),
            "saldo": sorted(
                (bid, cat.value, round(v, 6))
                for bid, cats in self.saldo_historico.items()
                for cat, v in cats.items()
            ),
            "proibicoes": sorted((b, d.isoformat()) for b, d in self.proibicoes),
            "excecoes": sorted(
                (b, d.isoformat()) for b, d in self.excecoes_descanso
            ),
        }
        bruto = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(bruto).hexdigest()


# --------------------------------------------------------------------------- #
# Saídas
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Plantao:
    data: date
    bombeiro_id: int
    tipo: str  # "branca" | "vermelha"
    categorias: tuple[Categoria, ...]
    origem: OrigemPlantao = OrigemPlantao.SOLVER
    travado: bool = False


@dataclass(frozen=True)
class ResultadoEstagio:
    codigo: str
    descricao: str
    valor: int
    valor_legivel: str


@dataclass
class Conflito:
    """Conjunto mínimo de restrições em conflito (Parte 2, infactibilidade)."""

    descricao: str
    referencias: list[str] = field(default_factory=list)


@dataclass
class ResultadoSolve:
    status: StatusSolve
    plantoes: list[Plantao] = field(default_factory=list)
    estagios: list[ResultadoEstagio] = field(default_factory=list)
    conflitos: list[Conflito] = field(default_factory=list)
    saldo_final: dict[int, dict[Categoria, float]] = field(default_factory=dict)
    preferencias_atendidas: int = 0
    preferencias_total: int = 0
    tempo_s: float = 0.0
    hash_entrada: str = ""

    @property
    def viavel(self) -> bool:
        return self.status in (StatusSolve.OTIMO, StatusSolve.VIAVEL)

    def escala_por_dia(self) -> dict[date, int]:
        return {p.data: p.bombeiro_id for p in self.plantoes}
