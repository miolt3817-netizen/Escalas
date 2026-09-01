"""Autenticação e permissões — JWT + RBAC para os três perfis.

Regra de visibilidade (Parte 2): o bombeiro vê a escala inteira — é informação
operacional — mas só edita as próprias indisponibilidades e preferências.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from . import modelos as m
from .banco import obter_sessao

AMBIENTE = os.getenv("AMBIENTE", "desenvolvimento")
SEGREDO = os.getenv("JWT_SECRET", "")

if not SEGREDO:
    if AMBIENTE == "producao":
        # Falha alto em vez de aceitar um segredo previsível: com JWT_SECRET
        # conhecido, qualquer pessoa forja um token de administrador.
        raise RuntimeError(
            "JWT_SECRET não definido. Em produção é obrigatório. "
            "Gere um com: python -c \"import secrets;print(secrets.token_urlsafe(48))\""
        )
    SEGREDO = "desenvolvimento-inseguro-nao-usar-em-producao"

ALGORITMO = "HS256"
EXPIRACAO_MIN = int(os.getenv("JWT_EXPIRACAO_MIN", "480"))

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

#: bcrypt trunca em 72 bytes. Truncamos explicitamente para que senhas longas
#: falhem de forma previsível em vez de estourar erro dentro da biblioteca.
LIMITE_BCRYPT = 72

ADMINISTRADOR = "administrador"
SUPERVISOR = "supervisor"
BOMBEIRO = "bombeiro"


def _bytes(senha: str) -> bytes:
    return senha.encode("utf-8")[:LIMITE_BCRYPT]


def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(_bytes(senha), bcrypt.gensalt()).decode("utf-8")


def conferir_senha(senha: str, hash_armazenado: str) -> bool:
    try:
        return bcrypt.checkpw(_bytes(senha), hash_armazenado.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def criar_token(usuario: m.Usuario) -> str:
    expira = datetime.now(UTC) + timedelta(minutes=EXPIRACAO_MIN)
    payload = {
        "sub": str(usuario.id),
        "papel": usuario.papel,
        "nome": usuario.nome,
        "exp": expira,
    }
    return jwt.encode(payload, SEGREDO, algorithm=ALGORITMO)


def usuario_atual(
    token: str | None = Depends(oauth2), db: Session = Depends(obter_sessao)
) -> m.Usuario:
    erro = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciais inválidas.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise erro
    try:
        payload = jwt.decode(token, SEGREDO, algorithms=[ALGORITMO])
        usuario_id = int(payload.get("sub", 0))
    except (JWTError, ValueError):
        raise erro from None

    usuario = db.get(m.Usuario, usuario_id)
    if usuario is None or not usuario.ativo:
        raise erro
    return usuario


def exigir(*papeis: str):
    """Dependência de rota: exige um dos papéis informados."""

    def verificador(usuario: m.Usuario = Depends(usuario_atual)) -> m.Usuario:
        if usuario.papel not in papeis:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Ação restrita a: {', '.join(papeis)}. "
                    f"Seu perfil: {usuario.papel}."
                ),
            )
        return usuario

    return verificador
