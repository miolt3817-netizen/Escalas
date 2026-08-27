"""Modelo de dados — Parte 2, "Modelo de dados".

Três decisões estruturais implementadas aqui:

1. VERSIONAMENTO: uma escala publicada nunca é sobrescrita. Regenerar cria
   nova versão e a anterior passa a "substituida". Unicidade parcial garante
   uma única versão publicada por mês/ano.

2. `plantoes` É A ÚNICA FONTE DA VERDADE do saldo de equidade. Não existe
   tabela `indice_equidade` — o saldo é derivado (view materializada em
   produção, cálculo em Python nos testes). Um livro-caixa paralelo
   dessincronizaria com trocas e ajustes feitos após a publicação.

3. AUDITORIA POR TRIGGER (infra/init.sql), não por hook de ORM — hooks não
   capturam UPDATE em massa, SQL cru nem edição direta no banco.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    senha_hash: Mapped[str] = mapped_column(String(255))
    #: administrador | supervisor | bombeiro
    papel: Mapped[str] = mapped_column(String(20), index=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    #: Senha inicial gerada pelo sistema: obriga troca no primeiro acesso.
    precisa_trocar_senha: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Parametro(Base):
    """Valores da Parte 0 — configuráveis, nunca constantes no código."""

    __tablename__ = "parametros"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(String(120))
    descricao: Mapped[str] = mapped_column(Text, default="", server_default="")


class Indisponibilidade(Base):
    __tablename__ = "indisponibilidades"

    id: Mapped[int] = mapped_column(primary_key=True)
    bombeiro_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    inicio: Mapped[date] = mapped_column(Date)
    fim: Mapped[date] = mapped_column(Date)
    #: ferias | licenca | atestado | afastamento
    tipo: Mapped[str] = mapped_column(String(20))
    observacao: Mapped[str] = mapped_column(Text, default="", server_default="")

    __table_args__ = (Index("ix_indisp_periodo", "inicio", "fim"),)


class Preferencia(Base):
    """Tabela ausente na v1 — o estágio E6 não tinha de onde ler os dados."""

    __tablename__ = "preferencias"

    id: Mapped[int] = mapped_column(primary_key=True)
    bombeiro_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    #: quer | evita
    tipo: Mapped[str] = mapped_column(String(10))
    data: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: 0 = segunda ... 6 = domingo
    dia_semana: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peso: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class Feriado(Base):
    __tablename__ = "feriados"

    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[date] = mapped_column(Date, unique=True, index=True)
    nome: Mapped[str] = mapped_column(String(120), default="", server_default="")
    ambito: Mapped[str] = mapped_column(String(20), default="nacional", server_default="nacional")


class Escala(Base):
    __tablename__ = "escalas"

    id: Mapped[int] = mapped_column(primary_key=True)
    ano: Mapped[int] = mapped_column(Integer, index=True)
    mes: Mapped[int] = mapped_column(Integer, index=True)
    versao: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    #: rascunho | publicada | substituida
    status: Mapped[str] = mapped_column(
        String(20), default="rascunho", server_default="rascunho", index=True
    )
    criada_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    publicada_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    criada_por: Mapped[int | None] = mapped_column(
        ForeignKey("usuarios.id"), nullable=True
    )

    plantoes: Mapped[list["Plantao"]] = relationship(
        back_populates="escala", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("ano", "mes", "versao", name="uq_escala_versao"),
    )


class Plantao(Base):
    """Fonte única da verdade sobre quem trabalhou quando."""

    __tablename__ = "plantoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    escala_id: Mapped[int] = mapped_column(ForeignKey("escalas.id"), index=True)
    data: Mapped[date] = mapped_column(Date, index=True)
    bombeiro_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    #: branca | vermelha
    tipo: Mapped[str] = mapped_column(String(10))
    #: solver | manual | troca
    origem: Mapped[str] = mapped_column(String(10), default="solver", server_default="solver")
    travado: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    observacoes: Mapped[str] = mapped_column(Text, default="", server_default="")

    escala: Mapped[Escala] = relationship(back_populates="plantoes")

    __table_args__ = (
        UniqueConstraint("escala_id", "data", name="uq_plantao_dia"),
    )


class Troca(Base):
    """Pedido de troca de plantão — Parte 1, "Trocas".

    Dois formatos:

    * **Cessão** — o solicitante quer se livrar de um dia e alguém assume.
      Muda um plantão. `plantao_oferecido_id` fica nulo.
    * **Permuta** — os dois trocam os dias entre si. Muda dois plantões.
      `plantao_oferecido_id` aponta para o dia que o aceitante entrega.

    Fluxo: solicitada -> aceita -> aprovada. Recusada ou cancelada encerram.
    """

    __tablename__ = "trocas"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Plantão que o solicitante quer entregar.
    plantao_id: Mapped[int] = mapped_column(ForeignKey("plantoes.id"), index=True)
    #: Plantão que o aceitante entrega em troca (permuta). Nulo em cessão.
    plantao_oferecido_id: Mapped[int | None] = mapped_column(
        ForeignKey("plantoes.id"), nullable=True
    )
    solicitante_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    aceitante_id: Mapped[int | None] = mapped_column(
        ForeignKey("usuarios.id"), nullable=True, index=True
    )
    aprovador_id: Mapped[int | None] = mapped_column(
        ForeignKey("usuarios.id"), nullable=True
    )
    #: solicitada | aceita | aprovada | recusada | cancelada
    status: Mapped[str] = mapped_column(
        String(20), default="solicitada", server_default="solicitada", index=True
    )
    motivo: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: Preenchido quando o supervisor recusa, ou quando a validação reprova.
    resposta: Mapped[str] = mapped_column(Text, default="", server_default="")
    criada_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolvida_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Explicacao(Base):
    """Explicação persistida junto com a escala.

    Refazer a explicação meses depois, com dados diferentes, daria outra
    resposta — por isso ela é gravada no momento da geração.
    """

    __tablename__ = "explicacoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    escala_id: Mapped[int] = mapped_column(ForeignKey("escalas.id"), index=True)
    #: mes | dia
    escopo: Mapped[str] = mapped_column(String(10))
    data: Mapped[date | None] = mapped_column(Date, nullable=True)
    fatos_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    texto: Mapped[str] = mapped_column(Text, default="", server_default="")
    criada_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SolveSnapshot(Base):
    """Snapshot das entradas do solve — sem isto, "por que o sistema decidiu
    assim em março?" não tem resposta verificável."""

    __tablename__ = "solve_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    escala_id: Mapped[int] = mapped_column(ForeignKey("escalas.id"), index=True)
    hash_entrada: Mapped[str] = mapped_column(String(64), index=True)
    entrada_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    estagios_json: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    tempo_s: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Excecao(Base):
    """Parte 0.5 — o supervisor autoriza conscientemente, com justificativa."""

    __tablename__ = "excecoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Opcional: a exceção costuma ser autorizada ANTES de existir escala —
    #: é justamente o que destrava a geração quando não há solução possível.
    escala_id: Mapped[int | None] = mapped_column(
        ForeignKey("escalas.id"), nullable=True, index=True
    )
    data: Mapped[date] = mapped_column(Date, index=True)
    bombeiro_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    #: Qual regra obrigatória foi dispensada. Hoje só H3 (descanso mínimo).
    regra_dispensada: Mapped[str] = mapped_column(
        String(10), default="H3", server_default="H3"
    )
    justificativa: Mapped[str] = mapped_column(Text)
    supervisor_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    criada_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Auditoria(Base):
    """Preenchida por TRIGGER no Postgres (infra/init.sql).

    O campo `motivo` — que o banco não tem como saber — é injetado pela
    aplicação via `SET LOCAL app.motivo`, lido pelo trigger.
    """

    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade: Mapped[str] = mapped_column(String(60), index=True)
    registro_id: Mapped[str] = mapped_column(String(40), index=True)
    operacao: Mapped[str] = mapped_column(String(10))
    usuario_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quando: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    antes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    depois: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    motivo: Mapped[str] = mapped_column(Text, default="", server_default="")


class Job(Base):
    """Tarefa em segundo plano — BackgroundTasks + polling.

    Celery/Redis entram quando (e só quando) o solve passar de ~10s, entrar
    envio de e-mail/push, ou surgir agendamento recorrente. Ver Parte 2.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(40))
    #: pendente | executando | concluido | falhou
    status: Mapped[str] = mapped_column(
        String(20), default="pendente", server_default="pendente", index=True
    )
    resultado: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    erro: Mapped[str] = mapped_column(Text, default="", server_default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    concluido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
