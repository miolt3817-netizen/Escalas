"""API — Parte 2, "Backend".

Processamento assíncrono: `BackgroundTasks` + polling em `/jobs/{id}`.
Celery + Redis entram quando o solve passar de ~10 s, entrar envio de
e-mail/push, ou surgir agendamento recorrente — não antes. Ver Parte 2.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from motor.calendario import dias_do_mes, mapa_categorias, tipo_do_dia
from motor.dominio import Categoria
from motor.explicacao import explicar_escolha, sugerir_substitutos
from motor.solver import resolver

from . import exportacao
from . import modelos as m
from . import servicos as s
from .banco import Sessao, criar_tabelas, definir_contexto_auditoria, obter_sessao
from .seed import gerar_senha
from .seguranca import (
    ADMINISTRADOR,
    BOMBEIRO,
    SUPERVISOR,
    conferir_senha,
    criar_token,
    exigir,
    hash_senha,
    usuario_atual,
)

#: Além disto, uma geração é considerada abandonada. O solve leva segundos;
#: minutos significam que o processo morreu no meio.
LIMITE_JOB_MINUTOS = 10


def _liberar_jobs_orfaos() -> None:
    """Marca como falhos os jobs que ficaram pendurados.

    `BackgroundTasks` morre junto com o processo: reiniciar o servidor no meio
    de uma geração deixaria o job em "executando" para sempre — e, como job em
    andamento bloqueia novas gerações daquele mês, o mês ficaria travado.
    """
    db = Sessao()
    try:
        presos = db.scalars(
            select(m.Job).where(m.Job.status.in_(("pendente", "executando")))
        ).all()
        for job in presos:
            job.status = "falhou"
            job.erro = "Interrompido: o servidor foi reiniciado durante a execução."
            job.concluido_em = datetime.now(UTC).replace(tzinfo=None)
        if presos:
            db.commit()
            print(f"[api] {len(presos)} job(s) interrompido(s) foram liberados.")
    finally:
        db.close()


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):  # pragma: no cover - infraestrutura
    criar_tabelas()
    _liberar_jobs_orfaos()
    yield


app = FastAPI(
    lifespan=ciclo_de_vida,
    title="EscalaFogo — sistema de escalas para bombeiros",
    description=(
        "Assistente inteligente do supervisor. O sistema não substitui o "
        "supervisor: automatiza a criação da escala, explica suas decisões e "
        "permite ajustes antes da publicação."
    ),
    version="2.0.0",
)


# --------------------------------------------------------------------------- #
# Esquemas
# --------------------------------------------------------------------------- #


class TokenResposta(BaseModel):
    access_token: str
    token_type: str = "bearer"
    papel: str
    nome: str
    precisa_trocar_senha: bool = False


class TrocaSenhaEntrada(BaseModel):
    senha_atual: str
    senha_nova: str = Field(min_length=8, max_length=72)


class UsuarioEntrada(BaseModel):
    nome: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=160)
    #: Opcional. Sem senha, o sistema gera uma e devolve uma única vez.
    senha: str | None = Field(default=None, min_length=8, max_length=72)
    papel: str = Field(pattern="^(administrador|supervisor|bombeiro)$")


class UsuarioCriado(BaseModel):
    id: int
    nome: str
    email: str
    papel: str
    #: Exibida uma única vez. Anote e entregue ao usuário.
    senha_inicial: str


class AtivacaoEntrada(BaseModel):
    ativo: bool


class UsuarioEdicao(BaseModel):
    nome: str | None = Field(default=None, min_length=2, max_length=120)
    email: str | None = Field(default=None, min_length=5, max_length=160)
    papel: str | None = Field(
        default=None, pattern="^(administrador|supervisor|bombeiro)$"
    )


class SenhaRedefinida(BaseModel):
    id: int
    nome: str
    email: str
    senha_inicial: str


class UsuarioSaida(BaseModel):
    id: int
    nome: str
    email: str
    papel: str
    ativo: bool
    precisa_trocar_senha: bool = False

    model_config = {"from_attributes": True}


class IndisponibilidadeEntrada(BaseModel):
    bombeiro_id: int
    inicio: date
    fim: date
    tipo: str = Field(pattern="^(ferias|licenca|atestado|afastamento)$")
    observacao: str = ""


class PreferenciaEntrada(BaseModel):
    bombeiro_id: int
    tipo: str = Field(pattern="^(quer|evita)$")
    data: date | None = None
    dia_semana: int | None = Field(default=None, ge=0, le=6)
    peso: int = 1


class FeriadoEntrada(BaseModel):
    data: date
    nome: str = ""
    ambito: str = "nacional"


class ParametroEntrada(BaseModel):
    chave: str
    valor: str
    descricao: str = ""


class GeracaoEntrada(BaseModel):
    ano: int = Field(ge=2000, le=2100)
    mes: int = Field(ge=1, le=12)


class AjusteEntrada(BaseModel):
    novo_bombeiro_id: int
    motivo: str = ""


class TrocaEntrada(BaseModel):
    #: Plantão que quero entregar (precisa ser meu).
    plantao_id: int
    #: Plantão que quero receber em troca. Sem ele, é cessão: alguém assume o
    #: meu dia sem me dar nada em troca.
    plantao_oferecido_id: int | None = None
    motivo: str = Field(default="", max_length=500)


class RespostaTroca(BaseModel):
    resposta: str = Field(default="", max_length=500)


class ExcecaoEntrada(BaseModel):
    """Parte 0.5 — autorização consciente para uma exceção específica."""

    data: date
    bombeiro_id: int
    #: Hoje só o descanso mínimo é dispensável. Cobrir todos os dias e não
    #: escalar quem está de férias não têm exceção: a primeira deixaria o
    #: quartel sem ninguém, a segunda é direito da pessoa.
    regra_dispensada: str = Field(default="H3", pattern="^H3$")
    justificativa: str = Field(min_length=10, max_length=500)


# --------------------------------------------------------------------------- #
# Autenticação
# --------------------------------------------------------------------------- #


@app.post("/auth/login", response_model=TokenResposta, tags=["auth"])
def login(
    form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(obter_sessao)
):
    usuario = db.scalars(
        select(m.Usuario).where(m.Usuario.email == form.username)
    ).first()
    if not usuario or not conferir_senha(form.password, usuario.senha_hash):
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    if not usuario.ativo:
        raise HTTPException(status_code=403, detail="Usuário inativo.")
    return TokenResposta(
        access_token=criar_token(usuario),
        papel=usuario.papel,
        nome=usuario.nome,
        precisa_trocar_senha=usuario.precisa_trocar_senha,
    )


@app.post("/auth/trocar-senha", tags=["auth"])
def trocar_senha(
    dados: TrocaSenhaEntrada,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Qualquer usuário troca a própria senha. Obrigatório no primeiro acesso."""
    if not conferir_senha(dados.senha_atual, usuario.senha_hash):
        raise HTTPException(status_code=401, detail="A senha atual não confere.")
    if dados.senha_nova == dados.senha_atual:
        raise HTTPException(
            status_code=400, detail="A senha nova precisa ser diferente da atual."
        )
    definir_contexto_auditoria(db, usuario.id, "troca de senha")
    usuario.senha_hash = hash_senha(dados.senha_nova)
    usuario.precisa_trocar_senha = False
    db.commit()
    return {"ok": True}


@app.get("/auth/eu", response_model=UsuarioSaida, tags=["auth"])
def eu(usuario: m.Usuario = Depends(usuario_atual)):
    return usuario


# --------------------------------------------------------------------------- #
# Administração
# --------------------------------------------------------------------------- #


@app.post("/usuarios", response_model=UsuarioCriado, tags=["equipe"])
def criar_usuario(
    dados: UsuarioEntrada,
    db: Session = Depends(obter_sessao),
    autor: m.Usuario = Depends(exigir(ADMINISTRADOR, SUPERVISOR)),
):
    """Cadastro de bombeiros — restrito a administrador e supervisor.

    O supervisor cadastra bombeiros; só o administrador cria outros
    administradores ou supervisores.
    """
    if autor.papel == SUPERVISOR and dados.papel != BOMBEIRO:
        raise HTTPException(
            status_code=403,
            detail="Supervisor cadastra apenas bombeiros. Peça ao administrador.",
        )
    email = dados.email.strip().lower()
    if db.scalars(select(m.Usuario).where(m.Usuario.email == email)).first():
        raise HTTPException(status_code=409, detail="Este e-mail já está cadastrado.")

    senha = dados.senha or gerar_senha()
    definir_contexto_auditoria(db, autor.id, f"cadastro de {dados.papel}")
    usuario = m.Usuario(
        nome=dados.nome.strip(),
        email=email,
        senha_hash=hash_senha(senha),
        papel=dados.papel,
        precisa_trocar_senha=True,
    )
    db.add(usuario)
    db.commit()
    return UsuarioCriado(
        id=usuario.id,
        nome=usuario.nome,
        email=usuario.email,
        papel=usuario.papel,
        senha_inicial=senha,
    )


def _pode_gerenciar(autor: m.Usuario, alvo: m.Usuario) -> None:
    """Supervisor mexe só em bombeiros; ninguém mexe na própria conta."""
    if alvo.id == autor.id:
        raise HTTPException(
            status_code=400, detail="Você não pode alterar a própria conta por aqui."
        )
    if autor.papel == SUPERVISOR and alvo.papel != BOMBEIRO:
        raise HTTPException(
            status_code=403, detail="Supervisor só altera contas de bombeiros."
        )


@app.put("/usuarios/{usuario_id}", response_model=UsuarioSaida, tags=["equipe"])
def editar_usuario(
    usuario_id: int,
    dados: UsuarioEdicao,
    db: Session = Depends(obter_sessao),
    autor: m.Usuario = Depends(exigir(ADMINISTRADOR, SUPERVISOR)),
):
    """Corrigir nome, e-mail ou perfil de alguém já cadastrado."""
    alvo = db.get(m.Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    _pode_gerenciar(autor, alvo)

    if dados.papel and autor.papel == SUPERVISOR and dados.papel != BOMBEIRO:
        raise HTTPException(
            status_code=403, detail="Supervisor não promove ninguém a supervisor ou administrador."
        )
    if dados.email:
        email = dados.email.strip().lower()
        existente = db.scalars(
            select(m.Usuario).where(m.Usuario.email == email)
        ).first()
        if existente and existente.id != alvo.id:
            raise HTTPException(
                status_code=409, detail="Este e-mail já está em uso por outra pessoa."
            )
        alvo.email = email

    definir_contexto_auditoria(db, autor.id, "edição de cadastro")
    if dados.nome:
        alvo.nome = dados.nome.strip()
    if dados.papel:
        alvo.papel = dados.papel
    db.commit()
    return alvo


@app.post(
    "/usuarios/{usuario_id}/redefinir-senha",
    response_model=SenhaRedefinida,
    tags=["equipe"],
)
def redefinir_senha(
    usuario_id: int,
    db: Session = Depends(obter_sessao),
    autor: m.Usuario = Depends(exigir(ADMINISTRADOR, SUPERVISOR)),
):
    """Gera uma senha temporária — para quando alguém esquece a própria.

    Não há recuperação por e-mail: quem gerencia entrega a senha nova, e a
    pessoa escolhe outra no acesso seguinte.
    """
    alvo = db.get(m.Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    _pode_gerenciar(autor, alvo)

    senha = gerar_senha()
    definir_contexto_auditoria(db, autor.id, "redefinição de senha")
    alvo.senha_hash = hash_senha(senha)
    alvo.precisa_trocar_senha = True
    db.commit()
    return SenhaRedefinida(
        id=alvo.id, nome=alvo.nome, email=alvo.email, senha_inicial=senha
    )


@app.delete("/usuarios/{usuario_id}", tags=["equipe"])
def excluir_usuario(
    usuario_id: int,
    db: Session = Depends(obter_sessao),
    autor: m.Usuario = Depends(exigir(ADMINISTRADOR, SUPERVISOR)),
):
    """Exclui de vez — mas só quem nunca entrou numa escala.

    Quem já tem plantão registrado NÃO pode ser excluído: apagar quebraria o
    histórico de quem trabalhou quando, que é registro trabalhista. Nesse caso
    o caminho é desativar, e a resposta explica isso.
    """
    alvo = db.get(m.Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    _pode_gerenciar(autor, alvo)

    plantoes = db.scalars(
        select(m.Plantao).where(m.Plantao.bombeiro_id == usuario_id)
    ).all()
    if plantoes:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{alvo.nome} já trabalhou em {len(plantoes)} plantão(ões). "
                "Excluir apagaria o histórico de escalas. Use 'Desativar': a "
                "pessoa sai das próximas escalas e o histórico fica intacto."
            ),
        )

    definir_contexto_auditoria(db, autor.id, "exclusão de cadastro")
    for tabela in (m.Indisponibilidade, m.Preferencia):
        for registro in db.scalars(
            select(tabela).where(tabela.bombeiro_id == usuario_id)
        ).all():
            db.delete(registro)
    nome = alvo.nome
    db.delete(alvo)
    db.commit()
    return {"ok": True, "mensagem": f"{nome} foi excluído(a) do sistema."}


@app.patch("/usuarios/{usuario_id}", response_model=UsuarioSaida, tags=["equipe"])
def ativar_desativar(
    usuario_id: int,
    dados: AtivacaoEntrada,
    db: Session = Depends(obter_sessao),
    autor: m.Usuario = Depends(exigir(ADMINISTRADOR, SUPERVISOR)),
):
    """Desativa em vez de excluir: o histórico de plantões precisa ser mantido.

    Bombeiro inativo deixa de entrar em novas escalas, mas as escalas passadas
    continuam íntegras e o saldo de equidade histórico é preservado.
    """
    alvo = db.get(m.Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    _pode_gerenciar(autor, alvo)
    definir_contexto_auditoria(
        db, autor.id, "ativação" if dados.ativo else "desativação de conta"
    )
    alvo.ativo = dados.ativo
    db.commit()
    return alvo


@app.get("/usuarios", response_model=list[UsuarioSaida], tags=["admin"])
def listar_usuarios(
    papel: str | None = None,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(usuario_atual),
):
    consulta = select(m.Usuario)
    if papel:
        consulta = consulta.where(m.Usuario.papel == papel)
    return db.scalars(consulta.order_by(m.Usuario.nome)).all()


@app.get("/parametros", tags=["admin"])
def listar_parametros(
    db: Session = Depends(obter_sessao), _: m.Usuario = Depends(usuario_atual)
):
    atuais = s.carregar_parametros(db)
    return {
        "efetivos": atuais.__dict__,
        "armazenados": {
            p.chave: p.valor for p in db.scalars(select(m.Parametro)).all()
        },
    }


@app.put("/parametros", tags=["admin"])
def definir_parametro(
    dados: ParametroEntrada,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(exigir(ADMINISTRADOR)),
):
    if dados.chave not in s.CAMPOS_PARAMETROS:
        raise HTTPException(
            status_code=400,
            detail=f"Parâmetro desconhecido. Válidos: {sorted(s.CAMPOS_PARAMETROS)}",
        )
    existente = db.get(m.Parametro, dados.chave)
    if existente:
        existente.valor = dados.valor
        existente.descricao = dados.descricao or existente.descricao
    else:
        db.add(m.Parametro(**dados.model_dump()))
    db.commit()
    return {"ok": True, "chave": dados.chave, "valor": dados.valor}


@app.post("/feriados", tags=["admin"])
def criar_feriado(
    dados: FeriadoEntrada,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(exigir(ADMINISTRADOR, SUPERVISOR)),
):
    if db.scalars(select(m.Feriado).where(m.Feriado.data == dados.data)).first():
        raise HTTPException(status_code=409, detail="Feriado já cadastrado.")
    feriado = m.Feriado(**dados.model_dump())
    db.add(feriado)
    db.commit()
    return {"id": feriado.id, "data": feriado.data, "nome": feriado.nome}


@app.get("/feriados", tags=["admin"])
def listar_feriados(
    db: Session = Depends(obter_sessao), _: m.Usuario = Depends(usuario_atual)
):
    return db.scalars(select(m.Feriado).order_by(m.Feriado.data)).all()


# --------------------------------------------------------------------------- #
# Indisponibilidades e preferências
# --------------------------------------------------------------------------- #


@app.post("/indisponibilidades", tags=["bombeiro"])
def criar_indisponibilidade(
    dados: IndisponibilidadeEntrada,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    if usuario.papel == BOMBEIRO and dados.bombeiro_id != usuario.id:
        raise HTTPException(
            status_code=403, detail="Bombeiro só registra a própria indisponibilidade."
        )
    if dados.fim < dados.inicio:
        raise HTTPException(
            status_code=400, detail="A data final é anterior à inicial."
        )
    if (dados.fim - dados.inicio).days > 400:
        raise HTTPException(status_code=400, detail="Período maior que um ano.")

    definir_contexto_auditoria(db, usuario.id, f"indisponibilidade: {dados.tipo}")
    registro = m.Indisponibilidade(**dados.model_dump())
    db.add(registro)
    db.commit()

    # Se o período cobre plantões de uma escala já publicada, avisa: é o caso
    # de "Imprevistos" da especificação e exige ação do supervisor.
    conflitos = [
        p.data.isoformat()
        for p in db.scalars(
            select(m.Plantao)
            .join(m.Escala, m.Escala.id == m.Plantao.escala_id)
            .where(
                m.Plantao.bombeiro_id == dados.bombeiro_id,
                m.Plantao.data >= dados.inicio,
                m.Plantao.data <= dados.fim,
                m.Escala.status == "publicada",
            )
            .order_by(m.Plantao.data)
        ).all()
    ]
    return {"id": registro.id, "plantoes_em_conflito": conflitos}


@app.get("/indisponibilidades", tags=["bombeiro"])
def listar_indisponibilidades(
    bombeiro_id: int | None = None,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    consulta = select(m.Indisponibilidade)
    # Bombeiro vê apenas as próprias; supervisor e administrador veem todas.
    if usuario.papel == BOMBEIRO:
        consulta = consulta.where(m.Indisponibilidade.bombeiro_id == usuario.id)
    elif bombeiro_id is not None:
        consulta = consulta.where(m.Indisponibilidade.bombeiro_id == bombeiro_id)
    return db.scalars(consulta.order_by(m.Indisponibilidade.inicio.desc())).all()


@app.delete("/indisponibilidades/{registro_id}", tags=["bombeiro"])
def remover_indisponibilidade(
    registro_id: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    registro = db.get(m.Indisponibilidade, registro_id)
    if registro is None:
        raise HTTPException(status_code=404, detail="Registro não encontrado.")
    if usuario.papel == BOMBEIRO and registro.bombeiro_id != usuario.id:
        raise HTTPException(
            status_code=403, detail="Você só remove os próprios registros."
        )
    definir_contexto_auditoria(db, usuario.id, "remoção de indisponibilidade")
    db.delete(registro)
    db.commit()
    return {"ok": True}


@app.post("/preferencias", tags=["bombeiro"])
def criar_preferencia(
    dados: PreferenciaEntrada,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    if usuario.papel == BOMBEIRO and dados.bombeiro_id != usuario.id:
        raise HTTPException(
            status_code=403, detail="Bombeiro só registra as próprias preferências."
        )
    if dados.data is None and dados.dia_semana is None:
        raise HTTPException(
            status_code=400, detail="Informe uma data ou um dia da semana."
        )
    if dados.data is not None and dados.dia_semana is not None:
        raise HTTPException(
            status_code=400,
            detail="Escolha uma data OU um dia da semana, não os dois.",
        )
    definir_contexto_auditoria(db, usuario.id, f"preferência: {dados.tipo}")
    registro = m.Preferencia(**dados.model_dump())
    db.add(registro)
    db.commit()
    return {"id": registro.id}


@app.get("/preferencias", tags=["bombeiro"])
def listar_preferencias(
    bombeiro_id: int | None = None,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    consulta = select(m.Preferencia)
    if usuario.papel == BOMBEIRO:
        consulta = consulta.where(m.Preferencia.bombeiro_id == usuario.id)
    elif bombeiro_id is not None:
        consulta = consulta.where(m.Preferencia.bombeiro_id == bombeiro_id)
    return db.scalars(consulta.order_by(m.Preferencia.id.desc())).all()


@app.delete("/preferencias/{registro_id}", tags=["bombeiro"])
def remover_preferencia(
    registro_id: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    registro = db.get(m.Preferencia, registro_id)
    if registro is None:
        raise HTTPException(status_code=404, detail="Registro não encontrado.")
    if usuario.papel == BOMBEIRO and registro.bombeiro_id != usuario.id:
        raise HTTPException(
            status_code=403, detail="Você só remove os próprios registros."
        )
    definir_contexto_auditoria(db, usuario.id, "remoção de preferência")
    db.delete(registro)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Geração da escala (job em segundo plano + polling)
# --------------------------------------------------------------------------- #


def _executar_geracao(job_id: str, ano: int, mes: int, usuario_id: int) -> None:
    db = Sessao()
    try:
        job = db.get(m.Job, job_id)
        job.status = "executando"
        db.commit()

        definir_contexto_auditoria(db, usuario_id, f"geração de escala {mes:02d}/{ano}")
        escala, resultado, texto = s.gerar_escala(db, ano, mes, usuario_id)

        job = db.get(m.Job, job_id)
        if escala is None:
            job.status = "concluido"
            job.resultado = {
                "viavel": False,
                "conflitos": [c.descricao for c in resultado.conflitos],
                "hash_entrada": resultado.hash_entrada,
            }
        else:
            job.resultado = {
                "viavel": True,
                "escala_id": escala.id,
                "versao": escala.versao,
                "resumo": texto,
                "estagios": [
                    {"codigo": e.codigo, "descricao": e.descricao,
                     "valor_legivel": e.valor_legivel}
                    for e in resultado.estagios
                ],
                "tempo_s": round(resultado.tempo_s, 2),
                "hash_entrada": resultado.hash_entrada,
            }
            job.status = "concluido"
        job.concluido_em = datetime.now(UTC)
        db.commit()
    except Exception as erro:  # noqa: BLE001 - o job precisa registrar a falha
        db.rollback()
        job = db.get(m.Job, job_id)
        if job:
            job.status = "falhou"
            job.erro = str(erro)
            job.concluido_em = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


@app.post("/escalas/gerar", tags=["escala"])
def gerar(
    dados: GeracaoEntrada,
    tarefas: BackgroundTasks,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    """Uma geração por mês de cada vez.

    Sem isso, um duplo clique dispara duas gerações que competem pelo mesmo
    número de versão e uma quebra. Além de desperdiçar processamento, gerar
    duas versões idênticas do mesmo mês não serve para nada.
    """
    tipo = f"gerar_escala:{dados.ano}-{dados.mes:02d}"
    # Só bloqueia geração RECENTE: um job antigo pendurado significa processo
    # morto, e não pode impedir o supervisor de trabalhar para sempre.
    corte = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        minutes=LIMITE_JOB_MINUTOS
    )
    em_andamento = db.scalars(
        select(m.Job).where(
            m.Job.tipo == tipo,
            m.Job.status.in_(("pendente", "executando")),
            m.Job.criado_em >= corte,
        )
    ).first()
    if em_andamento is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "mensagem": (
                    f"Já existe uma geração em andamento para "
                    f"{dados.mes:02d}/{dados.ano}."
                ),
                "job_id": em_andamento.id,
            },
        )

    job = m.Job(id=str(uuid.uuid4()), tipo=tipo, status="pendente")
    db.add(job)
    db.commit()
    tarefas.add_task(_executar_geracao, job.id, dados.ano, dados.mes, usuario.id)
    return {"job_id": job.id, "status": "pendente"}


@app.get("/jobs/{job_id}", tags=["escala"])
def consultar_job(
    job_id: str,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(usuario_atual),
):
    job = db.get(m.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return {
        "id": job.id,
        "status": job.status,
        "resultado": job.resultado,
        "erro": job.erro,
    }


# --------------------------------------------------------------------------- #
# Consulta da escala
# --------------------------------------------------------------------------- #


@app.get("/escalas/{ano}/{mes}", tags=["escala"])
def obter_escala(
    ano: int,
    mes: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """O bombeiro só enxerga escala publicada; rascunho é do supervisor."""
    gerencia = usuario.papel in (SUPERVISOR, ADMINISTRADOR)
    escala = s.escala_vigente(db, ano, mes, incluir_rascunho=gerencia)
    if escala is None:
        detalhe = (
            f"Nenhuma escala para {mes:02d}/{ano}."
            if gerencia
            else f"A escala de {mes:02d}/{ano} ainda não foi publicada."
        )
        raise HTTPException(status_code=404, detail=detalhe)

    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}
    feriados = {
        f.data: f.nome
        for f in db.scalars(select(m.Feriado)).all()
    }
    explicacao = db.scalars(
        select(m.Explicacao).where(
            m.Explicacao.escala_id == escala.id, m.Explicacao.escopo == "mes"
        )
    ).first()
    # Os estágios ficam gravados no snapshot do solve: devolvê-los aqui faz o
    # painel "Como o motor decidiu" funcionar também ao navegar entre meses,
    # não só logo após gerar.
    snapshot = db.scalars(
        select(m.SolveSnapshot).where(m.SolveSnapshot.escala_id == escala.id)
    ).first()

    return {
        "id": escala.id,
        "ano": escala.ano,
        "mes": escala.mes,
        "versao": escala.versao,
        "status": escala.status,
        "resumo": explicacao.texto if explicacao else "",
        "estagios": (snapshot.estagios_json if snapshot else []) or [],
        "hash_entrada": snapshot.hash_entrada if snapshot else "",
        "plantoes": [
            {
                "id": p.id,
                "data": p.data.isoformat(),
                "dia_semana": p.data.strftime("%A"),
                "tipo": p.tipo,
                "bombeiro_id": p.bombeiro_id,
                "bombeiro": nomes.get(p.bombeiro_id, "?"),
                "feriado": feriados.get(p.data),
                "origem": p.origem,
                "travado": p.travado,
                "observacoes": p.observacoes,
            }
            for p in sorted(escala.plantoes, key=lambda x: x.data)
        ],
    }


@app.get("/escalas/{ano}/{mes}/exportar/{formato}", tags=["escala"])
def exportar(
    ano: int,
    mes: int,
    formato: str,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Exporta a escala em PDF, XLSX ou CSV — gerado no servidor.

    Gerar no navegador não dá controle sobre largura de coluna nem codificação
    de arquivo, e foi o que produziu CSV ilegível no Excel.
    """
    if formato not in exportacao.NOMES_ARQUIVO:
        raise HTTPException(
            status_code=400,
            detail=f"Formato inválido. Use: {', '.join(exportacao.NOMES_ARQUIVO)}.",
        )
    dados = obter_escala(ano, mes, db, usuario)
    plantoes = dados["plantoes"]

    if formato == "pdf":
        conteudo = exportacao.gerar_pdf(
            ano, mes, plantoes, dados["status"], dados["versao"], dados["resumo"]
        )
    elif formato == "xlsx":
        conteudo = exportacao.gerar_xlsx(
            ano, mes, plantoes, dados["status"], dados["versao"], dados["resumo"]
        )
    else:
        conteudo = exportacao.gerar_csv(ano, mes, plantoes)

    modelo, tipo = exportacao.NOMES_ARQUIVO[formato]
    nome = modelo.format(ano=ano, mes=mes)
    return Response(
        content=conteudo,
        media_type=tipo,
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@app.get("/escalas/{ano}/{mes}/dia/{dia}", tags=["escala"])
def detalhe_do_dia(
    ano: int,
    mes: int,
    dia: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Tudo sobre um dia: quem está, e quem poderia assumir.

    A lista de candidatos só vai para quem pode alterar a escala.
    """
    try:
        alvo = date(ano, mes, dia)
    except ValueError:
        raise HTTPException(status_code=422, detail="Data inválida.") from None

    gerencia = usuario.papel in (SUPERVISOR, ADMINISTRADOR)
    escala = s.escala_vigente(db, ano, mes, incluir_rascunho=gerencia)
    if escala is None:
        raise HTTPException(
            status_code=404, detail="Escala não encontrada ou não publicada."
        )

    plantao = next((p for p in escala.plantoes if p.data == alvo), None)
    if plantao is None:
        raise HTTPException(status_code=404, detail="Sem plantão nesta data.")

    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}
    feriado = db.scalars(select(m.Feriado).where(m.Feriado.data == alvo)).first()

    return {
        "plantao_id": plantao.id,
        "data": alvo.isoformat(),
        "tipo": plantao.tipo,
        "feriado": feriado.nome if feriado else None,
        "bombeiro_id": plantao.bombeiro_id,
        "bombeiro": nomes.get(plantao.bombeiro_id, "?"),
        "origem": plantao.origem,
        "travado": plantao.travado,
        "escala_status": escala.status,
        "passado": alvo < date.today(),
        "candidatos": s.candidatos_para(db, escala, alvo) if gerencia else [],
    }


@app.get("/escalas/{ano}/{mes}/versoes", tags=["escala"])
def listar_versoes(
    ano: int,
    mes: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Histórico de versões. O bombeiro vê apenas as que foram publicadas."""
    consulta = select(m.Escala).where(m.Escala.ano == ano, m.Escala.mes == mes)
    if usuario.papel == BOMBEIRO:
        consulta = consulta.where(m.Escala.status.in_(("publicada", "substituida")))
    escalas = db.scalars(consulta.order_by(m.Escala.versao.desc())).all()
    return [
        {
            "id": e.id,
            "versao": e.versao,
            "status": e.status,
            "criada_em": e.criada_em,
            "publicada_em": e.publicada_em,
        }
        for e in escalas
    ]


@app.post("/escalas/{escala_id}/publicar", tags=["escala"])
def publicar_escala(
    escala_id: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    definir_contexto_auditoria(db, usuario.id, "publicação de escala")
    try:
        escala = s.publicar(db, escala_id)
    except ValueError as erro:
        raise HTTPException(status_code=404, detail=str(erro)) from erro
    return {"id": escala.id, "status": escala.status, "versao": escala.versao}


@app.patch("/plantoes/{plantao_id}", tags=["escala"])
def ajustar(
    plantao_id: int,
    dados: AjusteEntrada,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    """Toda alteração é validada automaticamente (Parte 1, "Ajustes")."""
    if db.get(m.Plantao, plantao_id) is None:
        raise HTTPException(status_code=404, detail="Plantão não encontrado.")

    definir_contexto_auditoria(db, usuario.id, dados.motivo or "ajuste manual")
    ok, violacoes = s.ajustar_plantao(db, plantao_id, dados.novo_bombeiro_id)
    if not ok:
        raise HTTPException(
            status_code=422,
            detail={
                "mensagem": "A alteração violaria regras obrigatórias.",
                "violacoes": violacoes,
            },
        )
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Explicabilidade e estatísticas
# --------------------------------------------------------------------------- #


@app.get("/escalas/{ano}/{mes}/explicacao/{dia}", tags=["inteligencia"])
def explicar_dia(
    ano: int,
    mes: int,
    dia: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Re-solve contrafactual sob demanda. Nunca para os 30 dias de uma vez."""
    gerencia = usuario.papel in (SUPERVISOR, ADMINISTRADOR)
    escala = s.escala_vigente(db, ano, mes, incluir_rascunho=gerencia)
    if escala is None:
        raise HTTPException(
            status_code=404,
            detail="Escala não encontrada ou ainda não publicada.",
        )

    alvo = date(ano, mes, dia)
    guardada = db.scalars(
        select(m.Explicacao).where(
            m.Explicacao.escala_id == escala.id,
            m.Explicacao.escopo == "dia",
            m.Explicacao.data == alvo,
        )
    ).first()
    if guardada:
        return {"texto": guardada.texto, "fatos": guardada.fatos_json, "cache": True}

    entrada = s.montar_entrada(db, ano, mes, preservar_travados=False)
    resultado = resolver(entrada, validar=False)
    if not resultado.viavel:
        raise HTTPException(status_code=409, detail="Escala atual não é reprodutível.")

    fatos, texto = explicar_escolha(entrada, resultado, alvo)
    db.add(
        m.Explicacao(
            escala_id=escala.id,
            escopo="dia",
            data=alvo,
            fatos_json=fatos.json(),
            texto=texto,
        )
    )
    db.commit()
    return {"texto": texto, "fatos": fatos.json(), "cache": False}


@app.get("/escalas/{ano}/{mes}/substitutos/{dia}", tags=["inteligencia"])
def substitutos(
    ano: int,
    mes: int,
    dia: int,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    """Parte 1, "Imprevistos": melhores substitutos, ordenados por impacto."""
    entrada = s.montar_entrada(db, ano, mes, preservar_travados=False)
    resultado = resolver(entrada, validar=False)
    if not resultado.viavel:
        raise HTTPException(status_code=409, detail="Escala atual não é reprodutível.")
    return {"data": date(ano, mes, dia).isoformat(),
            "sugestoes": sugerir_substitutos(entrada, resultado, date(ano, mes, dia))}


@app.get("/equidade", tags=["inteligencia"])
def equidade(
    ano: int,
    mes: int,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(usuario_atual),
):
    """Saldo de equidade — DERIVADO de plantoes, nunca de um contador."""
    saldos = s.saldo_historico(db, ano, mes)
    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}
    # Quantas escalas publicadas alimentam este saldo. Zero não é erro: é o
    # estado inicial, antes da primeira publicação.
    corte = date(ano, mes, 1)
    publicadas = sum(
        1
        for e in db.scalars(
            select(m.Escala).where(m.Escala.status == "publicada")
        ).all()
        if date(e.ano, e.mes, 1) < corte
    )
    return {
        "referencia": f"{mes:02d}/{ano}",
        "escalas_publicadas": publicadas,
        "observacao": (
            "Saldo positivo = trabalhou acima da parcela justa. O cálculo é "
            "proporcional à disponibilidade: períodos de férias e licença não "
            "contam como dias elegíveis."
        ),
        "bombeiros": [
            {
                "bombeiro_id": bid,
                "nome": nomes.get(bid, "?"),
                "saldos": {c.value: round(v, 2) for c, v in cats.items()},
            }
            for bid, cats in sorted(saldos.items())
        ],
    }


@app.post("/escalas/{ano}/{mes}/excecoes", tags=["escala"])
def autorizar_excecao(
    ano: int,
    mes: int,
    dados: ExcecaoEntrada,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    """Libera uma regra obrigatória para UM bombeiro, num dia específico.

    Existe porque com efetivo pequeno e férias sobrepostas pode simplesmente
    não haver escala possível. O sistema nunca relaxa a regra sozinho — quem
    assume a decisão é uma pessoa, com nome e justificativa no registro.

    A liberação é pontual: vale para aquele par bombeiro/dia, e para mais nada.
    """
    if dados.data.year != ano or dados.data.month != mes:
        raise HTTPException(
            status_code=400, detail="A data não pertence ao mês informado."
        )
    alvo = db.get(m.Usuario, dados.bombeiro_id)
    if alvo is None or alvo.papel != BOMBEIRO:
        raise HTTPException(status_code=404, detail="Bombeiro não encontrado.")

    indisponivel = db.scalars(
        select(m.Indisponibilidade).where(
            m.Indisponibilidade.bombeiro_id == dados.bombeiro_id,
            m.Indisponibilidade.inicio <= dados.data,
            m.Indisponibilidade.fim >= dados.data,
        )
    ).first()
    if indisponivel:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{alvo.nome} está de {indisponivel.tipo} nesta data. "
                "Férias e licença não admitem exceção — são direito da pessoa."
            ),
        )

    ja_existe = db.scalars(
        select(m.Excecao).where(
            m.Excecao.data == dados.data,
            m.Excecao.bombeiro_id == dados.bombeiro_id,
            m.Excecao.regra_dispensada == dados.regra_dispensada,
        )
    ).first()
    if ja_existe:
        return {"id": ja_existe.id, "ja_existia": True}

    escala = s.escala_vigente(db, ano, mes)
    definir_contexto_auditoria(
        db, usuario.id, f"exceção autorizada: {dados.justificativa}"
    )
    excecao = m.Excecao(
        escala_id=escala.id if escala else None,
        data=dados.data,
        bombeiro_id=dados.bombeiro_id,
        regra_dispensada=dados.regra_dispensada,
        justificativa=dados.justificativa,
        supervisor_id=usuario.id,
    )
    db.add(excecao)
    db.commit()
    return {"id": excecao.id, "ja_existia": False}


@app.get("/escalas/{ano}/{mes}/excecoes", tags=["escala"])
def listar_excecoes(
    ano: int,
    mes: int,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}
    return [
        {
            "id": e.id,
            "data": e.data.isoformat(),
            "bombeiro": nomes.get(e.bombeiro_id, "?"),
            "bombeiro_id": e.bombeiro_id,
            "regra": e.regra_dispensada,
            "justificativa": e.justificativa,
            "autorizada_por": nomes.get(e.supervisor_id, "?"),
            "criada_em": e.criada_em,
        }
        for e in db.scalars(select(m.Excecao)).all()
        if e.data.year == ano and e.data.month == mes
    ]


@app.delete("/excecoes/{excecao_id}", tags=["escala"])
def revogar_excecao(
    excecao_id: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    excecao = db.get(m.Excecao, excecao_id)
    if excecao is None:
        raise HTTPException(status_code=404, detail="Exceção não encontrada.")
    definir_contexto_auditoria(db, usuario.id, "revogação de exceção")
    db.delete(excecao)
    db.commit()
    return {"ok": True}


@app.delete("/escalas/{ano}/{mes}/rascunho", tags=["escala"])
def descartar_rascunho(
    ano: int,
    mes: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    """Joga fora o rascunho e tudo que foi ajustado à mão nele.

    Diferente de gerar de novo, que PRESERVA os plantões travados. Aqui o
    supervisor está dizendo "esqueça o que eu mexi e comece do zero".

    Escala publicada nunca é tocada: para trocar uma que já vale, o caminho é
    gerar uma versão nova e publicá-la.
    """
    rascunhos = [
        e for e in db.scalars(
            select(m.Escala).where(
                m.Escala.ano == ano, m.Escala.mes == mes,
                m.Escala.status == "rascunho",
            )
        ).all()
    ]
    if not rascunhos:
        raise HTTPException(
            status_code=404,
            detail=f"Não há rascunho para {mes:02d}/{ano}. Escala publicada não é descartada.",
        )

    ajustes = sum(
        1 for e in rascunhos for p in e.plantoes if p.origem in ("manual", "troca")
    )
    definir_contexto_auditoria(db, usuario.id, "rascunho descartado pelo supervisor")
    for escala in rascunhos:
        # Explicações, snapshots e exceções apontam para a escala e não têm
        # cascade: sem apagar antes, sobrariam linhas órfãs referenciando um
        # registro que não existe mais.
        for tabela in (m.Explicacao, m.SolveSnapshot):
            for filho in db.scalars(
                select(tabela).where(tabela.escala_id == escala.id)
            ).all():
                db.delete(filho)
        for excecao in db.scalars(
            select(m.Excecao).where(m.Excecao.escala_id == escala.id)
        ).all():
            excecao.escala_id = None  # a autorização vale além do rascunho
        db.delete(escala)
    db.commit()
    return {
        "descartados": len(rascunhos),
        "ajustes_perdidos": ajustes,
    }


@app.get("/escalas/{ano}/{mes}/porque", tags=["inteligencia"])
def porque_esta_escala(
    ano: int,
    mes: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Por que o mês saiu assim — o retrato das restrições que existiam.

    Diferente da explicação de um dia, que roda um contrafactual, aqui não há
    cálculo novo: é a leitura do que estava dado quando a escala foi montada.
    Todo número vem dos plantões e cadastros, nenhum é estimado.
    """
    gerencia = usuario.papel in (SUPERVISOR, ADMINISTRADOR)
    escala = s.escala_vigente(db, ano, mes, incluir_rascunho=gerencia)
    if escala is None:
        raise HTTPException(
            status_code=404, detail="Escala não encontrada ou não publicada."
        )

    dias = dias_do_mes(ano, mes)
    feriados = {f.data: f.nome for f in db.scalars(select(m.Feriado)).all()}
    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}
    escalados = {p.data: p.bombeiro_id for p in escala.plantoes}
    ativos = s.bombeiros_ativos(db)

    vermelhos = [d for d in dias if d.weekday() >= 5 or d in feriados]

    # --- quem faltou, e quanto isso pesou --------------------------------
    ausencias = []
    for indisp in db.scalars(select(m.Indisponibilidade)).all():
        cobertos = [d for d in dias if indisp.inicio <= d <= indisp.fim]
        if cobertos:
            ausencias.append({
                "bombeiro": nomes.get(indisp.bombeiro_id, "?"),
                "tipo": indisp.tipo,
                "dias": len(cobertos),
                "de": max(indisp.inicio, dias[0]).isoformat(),
                "ate": min(indisp.fim, dias[-1]).isoformat(),
            })
    ausencias.sort(key=lambda a: -a["dias"])

    # --- saldo com que cada um entrou no mês ------------------------------
    saldos = s.saldo_historico(db, ano, mes)
    entrada_do_mes = sorted(
        (
            {
                "bombeiro": nomes.get(b.id, "?"),
                "saldo": round(saldos.get(b.id, {}).get(Categoria.TOTAL, 0.0), 2),
                "plantoes_no_mes": sum(
                    1 for d, bid in escalados.items() if bid == b.id
                ),
            }
            for b in ativos
        ),
        key=lambda linha: linha["saldo"],
    )

    # --- preferências: as atendidas e, sobretudo, as que não foram --------
    atendidas = 0
    frustradas = []
    for pref in db.scalars(select(m.Preferencia)).all():
        alvos = [
            d for d in dias
            if pref.data == d
            or (pref.dia_semana is not None and d.weekday() == pref.dia_semana)
        ]
        for d in alvos:
            escalado = escalados.get(d) == pref.bombeiro_id
            if (pref.tipo == "quer") == escalado:
                atendidas += 1
            elif pref.tipo == "evita" and escalado:
                frustradas.append({
                    "bombeiro": nomes.get(pref.bombeiro_id, "?"),
                    "data": d.isoformat(),
                    "tipo": "evita",
                })
    total_alvos = atendidas + len(frustradas)

    # --- dias apertados: pouca gente disponível ---------------------------
    apertados = []
    for d in dias:
        disponiveis = [
            b for b in ativos
            if not db.scalars(
                select(m.Indisponibilidade).where(
                    m.Indisponibilidade.bombeiro_id == b.id,
                    m.Indisponibilidade.inicio <= d,
                    m.Indisponibilidade.fim >= d,
                )
            ).first()
        ]
        if len(disponiveis) <= max(2, len(ativos) // 3):
            apertados.append({
                "data": d.isoformat(),
                "disponiveis": len(disponiveis),
                "escalado": nomes.get(escalados.get(d), "?"),
            })

    snapshot = db.scalars(
        select(m.SolveSnapshot).where(m.SolveSnapshot.escala_id == escala.id)
    ).first()

    return {
        "ano": ano,
        "mes": mes,
        "status": escala.status,
        "versao": escala.versao,
        "dias_no_mes": len(dias),
        "dias_vermelhos": len(vermelhos),
        "feriados": [
            {"data": d.isoformat(), "nome": feriados[d]}
            for d in dias if d in feriados
        ],
        "bombeiros_ativos": len(ativos),
        "ausencias": ausencias,
        "entrada_do_mes": entrada_do_mes,
        "preferencias": {
            "atendidas": atendidas,
            "total": total_alvos,
            "frustradas": frustradas[:8],
        },
        "dias_apertados": apertados,
        "estagios": (snapshot.estagios_json if snapshot else []) or [],
        "ajustes_manuais": sum(1 for p in escala.plantoes if p.origem == "manual"),
        "trocas_aplicadas": sum(1 for p in escala.plantoes if p.origem == "troca"),
    }


@app.get("/pendencias", tags=["inteligencia"])
def pendencias(
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """O que espera por você agora.

    Não há envio de e-mail nem push: o sistema é consultado, não empurra
    aviso. Isto aqui é o equivalente honesto — quem abre vê o que falta fazer,
    com um caminho direto para resolver.
    """
    itens: list[dict] = []
    gerencia = usuario.papel in (SUPERVISOR, ADMINISTRADOR)
    hoje = date.today()

    trocas = db.scalars(
        select(m.Troca).where(m.Troca.status.in_(("solicitada", "aceita")))
    ).all()
    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}

    for troca in trocas:
        plantao = db.get(m.Plantao, troca.plantao_id)
        quando = plantao.data.strftime("%d/%m") if plantao else "?"
        if gerencia and troca.status == "aceita":
            itens.append({
                "tipo": "troca_para_aprovar",
                "urgencia": "alta",
                "texto": (
                    f"Troca de {nomes.get(troca.solicitante_id, '?')} com "
                    f"{nomes.get(troca.aceitante_id, '?')} no dia {quando} "
                    "aguarda sua aprovação."
                ),
                "vista": "trocas",
            })
        elif not gerencia and troca.status == "solicitada":
            aberto_para_mim = (
                troca.aceitante_id == usuario.id
                or (troca.aceitante_id is None and troca.solicitante_id != usuario.id)
            )
            if aberto_para_mim:
                itens.append({
                    "tipo": "troca_para_aceitar",
                    "urgencia": "media",
                    "texto": (
                        f"{nomes.get(troca.solicitante_id, '?')} quer passar o "
                        f"plantão do dia {quando}."
                    ),
                    "vista": "trocas",
                })

    if gerencia:
        # Escala do mês seguinte ainda não publicada, com o mês virando.
        proximo = date(hoje.year + (hoje.month == 12), hoje.month % 12 + 1, 1)
        if s.escala_vigente(db, proximo.year, proximo.month, incluir_rascunho=False) is None:
            faltam = (proximo - hoje).days
            if faltam <= 15:
                rascunho = s.escala_vigente(db, proximo.year, proximo.month)
                itens.append({
                    "tipo": "escala_a_publicar",
                    "urgencia": "alta" if faltam <= 7 else "media",
                    "texto": (
                        f"A escala de {proximo.month:02d}/{proximo.year} "
                        + ("está em rascunho e ainda não foi publicada."
                           if rascunho else "ainda não foi gerada.")
                        + f" Faltam {faltam} dias para o mês começar."
                    ),
                    "vista": "escala",
                    "ano": proximo.year,
                    "mes": proximo.month,
                })

        # Indisponibilidade cobrindo plantão já publicado: alguém precisa cobrir.
        publicadas = {
            e.id: e for e in db.scalars(
                select(m.Escala).where(m.Escala.status == "publicada")
            ).all()
        }
        for indisp in db.scalars(select(m.Indisponibilidade)).all():
            if indisp.fim < hoje:
                continue
            conflitos = [
                p for e in publicadas.values() for p in e.plantoes
                if p.bombeiro_id == indisp.bombeiro_id
                and indisp.inicio <= p.data <= indisp.fim
                and p.data >= hoje
            ]
            if conflitos:
                dias = ", ".join(p.data.strftime("%d/%m") for p in conflitos[:4])
                itens.append({
                    "tipo": "conflito_de_ausencia",
                    "urgencia": "alta",
                    "texto": (
                        f"{nomes.get(indisp.bombeiro_id, '?')} está de "
                        f"{indisp.tipo} e continua escalado em {dias}."
                    ),
                    "vista": "escala",
                    "ano": conflitos[0].data.year,
                    "mes": conflitos[0].data.month,
                })

    ordem = {"alta": 0, "media": 1, "baixa": 2}
    itens.sort(key=lambda i: ordem.get(i["urgencia"], 3))
    return {"total": len(itens), "itens": itens}


@app.get("/estatisticas", tags=["inteligencia"])
def estatisticas(
    ano: int,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(usuario_atual),
):
    """Números do ano — Parte 1, "Estatísticas".

    Conta apenas escalas **publicadas**: rascunho pode ser descartado, e
    incluí-lo daria a impressão de trabalho que talvez nunca aconteça.
    """
    escalas = [
        e for e in db.scalars(
            select(m.Escala).where(m.Escala.status == "publicada")
        ).all()
        if e.ano == ano
    ]
    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}
    feriados = {f.data for f in db.scalars(select(m.Feriado)).all()}

    contagem: dict[int, dict[str, int]] = {}
    ajustes = plantoes_totais = 0
    dias_publicados: dict[date, int] = {}

    for escala in escalas:
        for p in escala.plantoes:
            plantoes_totais += 1
            dias_publicados[p.data] = p.bombeiro_id
            if p.origem == "manual":
                ajustes += 1
            linha = contagem.setdefault(
                p.bombeiro_id,
                {"total": 0, "branca": 0, "vermelha": 0,
                 "sabado": 0, "domingo": 0, "feriado": 0},
            )
            linha["total"] += 1
            linha[p.tipo] += 1
            if p.data.weekday() == 5:
                linha["sabado"] += 1
            if p.data.weekday() == 6:
                linha["domingo"] += 1
            if p.data in feriados:
                linha["feriado"] += 1

    # Só conta preferência que teve chance: dia existente em escala publicada.
    atendidas = total_pref = 0
    for pref in db.scalars(select(m.Preferencia)).all():
        for dia, escalado in dias_publicados.items():
            alvo = pref.data == dia or (
                pref.dia_semana is not None and dia.weekday() == pref.dia_semana
            )
            if not alvo:
                continue
            total_pref += 1
            if (pref.tipo == "quer") == (escalado == pref.bombeiro_id):
                atendidas += 1

    ids_do_ano = {e.id for e in escalas}
    trocas = db.scalars(select(m.Troca)).all()

    def _do_ano(troca: m.Troca) -> bool:
        plantao = db.get(m.Plantao, troca.plantao_id)
        return plantao is not None and plantao.escala_id in ids_do_ano

    return {
        "ano": ano,
        "meses_publicados": sorted(e.mes for e in escalas),
        "plantoes_totais": plantoes_totais,
        "ajustes_manuais": ajustes,
        "preferencias": {
            "atendidas": atendidas,
            "total": total_pref,
            "percentual": round(100 * atendidas / total_pref) if total_pref else None,
        },
        "trocas": {
            "aprovadas": sum(
                1 for t in trocas if t.status == "aprovada" and _do_ano(t)
            ),
            "pendentes": sum(
                1 for t in trocas if t.status in ("solicitada", "aceita")
            ),
        },
        "por_bombeiro": sorted(
            (
                {"bombeiro_id": bid, "nome": nomes.get(bid, "?"), **linha}
                for bid, linha in contagem.items()
            ),
            key=lambda linha: (-linha["total"], linha["nome"]),
        ),
    }


@app.get("/auditoria", tags=["inteligencia"])
def auditoria(
    limite: int = 100,
    db: Session = Depends(obter_sessao),
    _: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    return db.scalars(
        select(m.Auditoria).order_by(m.Auditoria.quando.desc()).limit(limite)
    ).all()


# --------------------------------------------------------------------------- #
# Trocas — Parte 1: solicita -> aceita -> aprova -> valida -> atualiza
# --------------------------------------------------------------------------- #


def _troca_em_json(db: Session, troca: m.Troca, nomes: dict[int, str]) -> dict:
    origem = db.get(m.Plantao, troca.plantao_id)
    oferecido = (
        db.get(m.Plantao, troca.plantao_oferecido_id)
        if troca.plantao_oferecido_id
        else None
    )
    return {
        "id": troca.id,
        "status": troca.status,
        "tipo": "permuta" if oferecido else "cessão",
        "solicitante_id": troca.solicitante_id,
        "solicitante": nomes.get(troca.solicitante_id, "?"),
        "aceitante_id": troca.aceitante_id,
        "aceitante": nomes.get(troca.aceitante_id) if troca.aceitante_id else None,
        "data": origem.data.isoformat() if origem else None,
        "tipo_escala": origem.tipo if origem else None,
        "data_oferecida": oferecido.data.isoformat() if oferecido else None,
        "motivo": troca.motivo,
        "resposta": troca.resposta,
        "criada_em": troca.criada_em,
    }


@app.post("/trocas", tags=["trocas"])
def solicitar_troca(
    dados: TrocaEntrada,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Bombeiro pede para passar um plantão seu a outra pessoa."""
    plantao = db.get(m.Plantao, dados.plantao_id)
    if plantao is None:
        raise HTTPException(status_code=404, detail="Plantão não encontrado.")

    dono = plantao.bombeiro_id
    if usuario.papel == BOMBEIRO and dono != usuario.id:
        raise HTTPException(
            status_code=403, detail="Você só pode oferecer os próprios plantões."
        )

    escala = db.get(m.Escala, plantao.escala_id)
    if escala is None or escala.status != "publicada":
        raise HTTPException(
            status_code=409,
            detail="Só é possível trocar plantões de uma escala publicada.",
        )
    if plantao.data < date.today():
        raise HTTPException(
            status_code=409, detail="Este plantão já passou."
        )

    aberta = db.scalars(
        select(m.Troca).where(
            m.Troca.plantao_id == plantao.id,
            m.Troca.status.in_(("solicitada", "aceita")),
        )
    ).first()
    if aberta is not None:
        raise HTTPException(
            status_code=409,
            detail="Já existe um pedido de troca em aberto para este plantão.",
        )

    if dados.plantao_oferecido_id:
        contraparte = db.get(m.Plantao, dados.plantao_oferecido_id)
        if contraparte is None:
            raise HTTPException(
                status_code=404, detail="O plantão pedido em troca não existe."
            )
        if contraparte.bombeiro_id == dono:
            raise HTTPException(
                status_code=400, detail="Os dois plantões são da mesma pessoa."
            )
        if contraparte.escala_id != plantao.escala_id:
            raise HTTPException(
                status_code=400,
                detail="Só é possível permutar dentro do mesmo mês.",
            )

    definir_contexto_auditoria(db, usuario.id, "pedido de troca")
    troca = m.Troca(
        plantao_id=plantao.id,
        plantao_oferecido_id=dados.plantao_oferecido_id,
        solicitante_id=dono,
        # Permuta com plantão nomeado já sai endereçada a quem o detém.
        aceitante_id=(
            db.get(m.Plantao, dados.plantao_oferecido_id).bombeiro_id
            if dados.plantao_oferecido_id
            else None
        ),
        status="solicitada",
        motivo=dados.motivo,
    )
    db.add(troca)
    db.commit()
    return {"id": troca.id, "status": troca.status}


@app.get("/trocas", tags=["trocas"])
def listar_trocas(
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Bombeiro vê as próprias e as abertas que pode assumir.

    Supervisor e administrador veem todas — precisam para aprovar.
    """
    nomes = {u.id: u.nome for u in db.scalars(select(m.Usuario)).all()}
    todas = db.scalars(select(m.Troca).order_by(m.Troca.criada_em.desc())).all()

    if usuario.papel in (SUPERVISOR, ADMINISTRADOR):
        visiveis = todas
    else:
        visiveis = [
            t for t in todas
            if usuario.id in (t.solicitante_id, t.aceitante_id)
            or (t.status == "solicitada" and t.aceitante_id is None)
        ]

    saida = [_troca_em_json(db, t, nomes) for t in visiveis]
    for item, troca in zip(saida, visiveis):
        item["sou_solicitante"] = troca.solicitante_id == usuario.id
        item["posso_aceitar"] = (
            troca.status == "solicitada"
            and troca.solicitante_id != usuario.id
            and (troca.aceitante_id is None or troca.aceitante_id == usuario.id)
            and usuario.papel == BOMBEIRO
        )
        item["posso_aprovar"] = (
            troca.status == "aceita"
            and usuario.papel in (SUPERVISOR, ADMINISTRADOR)
        )
    return saida


@app.post("/trocas/{troca_id}/aceitar", tags=["trocas"])
def aceitar_troca(
    troca_id: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """Outro bombeiro assume o plantão. Ainda depende do supervisor."""
    troca = db.get(m.Troca, troca_id)
    if troca is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    if troca.status != "solicitada":
        raise HTTPException(
            status_code=409, detail=f"Este pedido já está {troca.status}."
        )
    if troca.solicitante_id == usuario.id:
        raise HTTPException(
            status_code=400, detail="Você não pode aceitar o próprio pedido."
        )
    if troca.aceitante_id is not None and troca.aceitante_id != usuario.id:
        raise HTTPException(
            status_code=403, detail="Este pedido foi endereçado a outra pessoa."
        )

    definir_contexto_auditoria(db, usuario.id, "aceite de troca")
    troca.aceitante_id = usuario.id
    troca.status = "aceita"
    db.commit()

    # Aviso antecipado: a validação que vale é a da aprovação, mas mostrar o
    # problema agora evita o supervisor descobrir depois por ninguém.
    problemas = s.validar_troca(db, troca)
    return {
        "id": troca.id,
        "status": troca.status,
        "alerta": problemas or None,
    }


@app.post("/trocas/{troca_id}/aprovar", tags=["trocas"])
def aprovar_troca(
    troca_id: int,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(exigir(SUPERVISOR, ADMINISTRADOR)),
):
    """Valida e aplica. Se violar regra obrigatória, nada é alterado."""
    troca = db.get(m.Troca, troca_id)
    if troca is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    if troca.status != "aceita":
        raise HTTPException(
            status_code=409,
            detail=f"Só é possível aprovar pedido aceito. Este está {troca.status}.",
        )

    definir_contexto_auditoria(db, usuario.id, "aprovação de troca")
    ok, problemas = s.aplicar_troca(db, troca)
    if not ok:
        db.rollback()
        troca = db.get(m.Troca, troca_id)
        troca.resposta = " ".join(problemas)
        db.commit()
        raise HTTPException(
            status_code=422,
            detail={
                "mensagem": "A troca violaria regras obrigatórias.",
                "violacoes": problemas,
            },
        )

    troca.status = "aprovada"
    troca.aprovador_id = usuario.id
    troca.resolvida_em = datetime.now(UTC).replace(tzinfo=None)
    db.commit()
    return {"id": troca.id, "status": troca.status}


@app.post("/trocas/{troca_id}/recusar", tags=["trocas"])
def recusar_troca(
    troca_id: int,
    dados: RespostaTroca,
    db: Session = Depends(obter_sessao),
    usuario: m.Usuario = Depends(usuario_atual),
):
    """O supervisor recusa; o solicitante cancela o próprio pedido."""
    troca = db.get(m.Troca, troca_id)
    if troca is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    if troca.status in ("aprovada", "recusada", "cancelada"):
        raise HTTPException(
            status_code=409, detail=f"Este pedido já está {troca.status}."
        )

    gerencia = usuario.papel in (SUPERVISOR, ADMINISTRADOR)
    if not gerencia and troca.solicitante_id != usuario.id:
        raise HTTPException(
            status_code=403, detail="Você só cancela os próprios pedidos."
        )

    definir_contexto_auditoria(
        db, usuario.id, "recusa de troca" if gerencia else "cancelamento de troca"
    )
    troca.status = "recusada" if gerencia else "cancelada"
    troca.aprovador_id = usuario.id if gerencia else None
    troca.resposta = dados.resposta
    troca.resolvida_em = datetime.now(UTC).replace(tzinfo=None)
    db.commit()
    return {"id": troca.id, "status": troca.status}


@app.get("/saude", tags=["infra"])
def saude():
    return {"status": "ok", "versao": app.version}


_WEB = Path(__file__).resolve().parent.parent / "web"
_DEMO = _WEB / "index.html"


@app.get("/manifest.json", include_in_schema=False)
def manifest():  # pragma: no cover - arquivo estático
    """Identidade do aplicativo instalável (nome, ícones, cores)."""
    return FileResponse(
        _WEB / "manifest.json", media_type="application/manifest+json"
    )


@app.get("/sw.js", include_in_schema=False)
def serviço_worker():  # pragma: no cover - arquivo estático
    """Sem cache: um service worker antigo preso no navegador é difícil de
    diagnosticar e pode servir uma casca desatualizada por tempo indefinido."""
    return FileResponse(
        _WEB / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/estatico/{arquivo}", include_in_schema=False)
def estatico(arquivo: str):  # pragma: no cover - arquivos estáticos
    # Nome simples apenas: impede sair da pasta com "../".
    if not arquivo.replace("-", "").replace(".", "").replace("_", "").isalnum():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    caminho = (_WEB / "estatico" / arquivo).resolve()
    if not caminho.is_file() or _WEB not in caminho.parents:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    return FileResponse(caminho, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/", include_in_schema=False)
def demo():  # pragma: no cover - página estática
    if _DEMO.exists():
        # Sem cache: durante o desenvolvimento o navegador guardava o HTML
        # antigo e a pessoa via a tela anterior mesmo após atualizar o código.
        return FileResponse(
            _DEMO,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )
    return {"mensagem": "API no ar. Documentação em /docs."}
