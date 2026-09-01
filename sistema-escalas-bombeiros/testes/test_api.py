"""Teste de integração da API — fluxo completo do produto.

Roda em SQLite (os triggers de auditoria são específicos do Postgres e vivem
em infra/init.sql). Cobre: login, RBAC, cadastro, geração via job, publicação,
versionamento, ajuste com validação e saldo de equidade derivado.
"""

from __future__ import annotations

import os
import tempfile

from datetime import UTC, datetime

import pytest

os.environ["DATABASE_URL"] = (
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "teste.db")
)

from fastapi.testclient import TestClient  # noqa: E402

from api import modelos as m  # noqa: E402
from api.banco import Sessao, criar_tabelas  # noqa: E402
from api.main import app  # noqa: E402
from api.seguranca import hash_senha  # noqa: E402


@pytest.fixture(scope="module")
def cliente():
    criar_tabelas()
    db = Sessao()
    db.add(
        m.Usuario(
            nome="Admin",
            email="admin@cb.gov.br",
            senha_hash=hash_senha("senha123"),
            papel="administrador",
        )
    )
    db.add(
        m.Usuario(
            nome="Sgt. Supervisor",
            email="sup@cb.gov.br",
            senha_hash=hash_senha("senha123"),
            papel="supervisor",
        )
    )
    for i, nome in enumerate(
        ["João", "Maria", "Carlos", "Ana", "Pedro", "Lucia", "Rafael"], start=1
    ):
        db.add(
            m.Usuario(
                nome=nome,
                email=f"b{i}@cb.gov.br",
                senha_hash=hash_senha("senha123"),
                papel="bombeiro",
            )
        )
    db.add(m.Parametro(chave="tempo_limite_estagio_s", valor="5"))
    db.commit()
    db.close()
    return TestClient(app)


def _token(cliente, email):
    r = cliente.post(
        "/auth/login", data={"username": email, "password": "senha123"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_e_perfil(cliente):
    cab = _token(cliente, "sup@cb.gov.br")
    r = cliente.get("/auth/eu", headers=cab)
    assert r.status_code == 200
    assert r.json()["papel"] == "supervisor"


def test_sem_token_e_bloqueado(cliente):
    assert cliente.get("/escalas/2026/9").status_code == 401


def test_rbac_bombeiro_nao_gera_escala(cliente):
    cab = _token(cliente, "b1@cb.gov.br")
    r = cliente.post("/escalas/gerar", json={"ano": 2026, "mes": 9}, headers=cab)
    assert r.status_code == 403
    assert "supervisor" in r.json()["detail"]


def test_bombeiro_nao_registra_indisponibilidade_de_outro(cliente):
    cab = _token(cliente, "b1@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=cab).json()["id"]
    r = cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": eu + 1,
            "inicio": "2026-09-05",
            "fim": "2026-09-06",
            "tipo": "atestado",
        },
        headers=cab,
    )
    assert r.status_code == 403


def test_fluxo_completo(cliente):
    admin = _token(cliente, "admin@cb.gov.br")
    sup = _token(cliente, "sup@cb.gov.br")

    cliente.post(
        "/feriados",
        json={"data": "2026-09-07", "nome": "Independência"},
        headers=admin,
    )

    bombeiros = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()
    primeiro = bombeiros[0]["id"]

    cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": primeiro,
            "inicio": "2026-09-10",
            "fim": "2026-09-16",
            "tipo": "ferias",
        },
        headers=admin,
    )
    cliente.post(
        "/preferencias",
        json={"bombeiro_id": bombeiros[1]["id"], "tipo": "evita", "dia_semana": 6},
        headers=admin,
    )

    # --- geração via job + polling ---
    r = cliente.post("/escalas/gerar", json={"ano": 2026, "mes": 9}, headers=sup)
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    job = cliente.get(f"/jobs/{job_id}", headers=sup).json()
    assert job["status"] == "concluido", job
    assert job["resultado"]["viavel"] is True
    assert job["resultado"]["hash_entrada"]
    assert len(job["resultado"]["estagios"]) == 8  # E1..E6 (E5 tem 3 partes)

    # --- consulta ---
    escala = cliente.get("/escalas/2026/9", headers=sup).json()
    assert len(escala["plantoes"]) == 30
    assert escala["status"] == "rascunho"
    assert escala["versao"] == 1
    assert "gerada com sucesso" in escala["resumo"]

    # regra obrigatória: ninguém escalado durante as férias
    for p in escala["plantoes"]:
        if "2026-09-10" <= p["data"] <= "2026-09-16":
            assert p["bombeiro_id"] != primeiro

    # feriado marcado como vermelha
    dia7 = next(p for p in escala["plantoes"] if p["data"] == "2026-09-07")
    assert dia7["tipo"] == "vermelha"
    assert dia7["feriado"] == "Independência"

    # --- publicação ---
    r = cliente.post(f"/escalas/{escala['id']}/publicar", headers=sup)
    assert r.json()["status"] == "publicada"

    # --- nova geração cria VERSÃO nova, não sobrescreve ---
    job2 = cliente.post(
        "/escalas/gerar", json={"ano": 2026, "mes": 9}, headers=sup
    ).json()["job_id"]
    resultado2 = cliente.get(f"/jobs/{job2}", headers=sup).json()
    assert resultado2["resultado"]["versao"] == 2

    versoes = cliente.get("/escalas/2026/9/versoes", headers=sup).json()
    assert len(versoes) == 2
    assert sum(1 for v in versoes if v["status"] == "publicada") == 1


def test_ajuste_invalido_e_recusado(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2026/9", headers=sup).json()

    # tenta escalar, num dia qualquer, quem está de férias
    bombeiros = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()
    de_ferias = bombeiros[0]["id"]
    alvo = next(p for p in escala["plantoes"] if p["data"] == "2026-09-12")

    r = cliente.patch(
        f"/plantoes/{alvo['id']}",
        json={"novo_bombeiro_id": de_ferias, "motivo": "teste"},
        headers=sup,
    )
    assert r.status_code == 422
    detalhe = r.json()["detail"]
    assert any("ferias" in v for v in detalhe["violacoes"])


def test_ajuste_valido_e_aceito_e_trava_o_plantao(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2026/9", headers=sup).json()
    plantoes = {p["data"]: p for p in escala["plantoes"]}

    alvo = plantoes["2026-09-20"]
    vizinhos = {plantoes["2026-09-19"]["bombeiro_id"],
                plantoes["2026-09-21"]["bombeiro_id"],
                alvo["bombeiro_id"]}
    bombeiros = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()
    candidato = next(
        b["id"]
        for b in bombeiros
        if b["id"] not in vizinhos and b["id"] != bombeiros[0]["id"]
    )

    r = cliente.patch(
        f"/plantoes/{alvo['id']}",
        json={"novo_bombeiro_id": candidato, "motivo": "pedido pessoal"},
        headers=sup,
    )
    assert r.status_code == 200, r.text

    escala = cliente.get("/escalas/2026/9", headers=sup).json()
    novo = next(p for p in escala["plantoes"] if p["data"] == "2026-09-20")
    assert novo["bombeiro_id"] == candidato
    assert novo["travado"] is True
    assert novo["origem"] == "manual"


def test_equidade_derivada(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    r = cliente.get("/equidade?ano=2026&mes=10", headers=sup)
    assert r.status_code == 200
    dados = r.json()
    assert len(dados["bombeiros"]) == 7
    # setembro publicado -> saldos existem e somam ~0 por construção
    total = sum(b["saldos"]["total"] for b in dados["bombeiros"])
    assert abs(total) < 0.05


def test_explicacao_de_dia(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    r = cliente.get("/escalas/2026/9/explicacao/12", headers=sup)
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["fatos"]["veredito"] in (
        "unica_opcao",
        "melhor_opcao",
        "equivalente",
    )
    assert len(corpo["texto"]) > 20
    # segunda chamada vem do cache persistido
    assert cliente.get("/escalas/2026/9/explicacao/12", headers=sup).json()["cache"]


def test_geracao_infactivel_reporta_conflito(cliente):
    admin = _token(cliente, "admin@cb.gov.br")
    sup = _token(cliente, "sup@cb.gov.br")
    bombeiros = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()

    for b in bombeiros:
        cliente.post(
            "/indisponibilidades",
            json={
                "bombeiro_id": b["id"],
                "inicio": "2026-11-15",
                "fim": "2026-11-15",
                "tipo": "atestado",
            },
            headers=admin,
        )

    job = cliente.post(
        "/escalas/gerar", json={"ano": 2026, "mes": 11}, headers=sup
    ).json()["job_id"]
    resultado = cliente.get(f"/jobs/{job}", headers=sup).json()["resultado"]

    assert resultado["viavel"] is False
    assert any("15/11/2026" in c for c in resultado["conflitos"])
    # e nenhuma escala foi criada
    assert cliente.get("/escalas/2026/11", headers=sup).status_code == 404


def test_troca_de_senha(cliente):
    """Primeiro acesso obriga troca; a senha antiga deixa de funcionar."""
    db = Sessao()
    db.add(
        m.Usuario(
            nome="Novato",
            email="novato@cb.gov.br",
            senha_hash=hash_senha("temporaria123"),
            papel="bombeiro",
            precisa_trocar_senha=True,
        )
    )
    db.commit()
    db.close()

    r = cliente.post(
        "/auth/login",
        data={"username": "novato@cb.gov.br", "password": "temporaria123"},
    )
    assert r.status_code == 200
    assert r.json()["precisa_trocar_senha"] is True
    cab = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # senha atual errada é recusada
    assert cliente.post(
        "/auth/trocar-senha",
        json={"senha_atual": "chutando", "senha_nova": "SenhaNova2026"},
        headers=cab,
    ).status_code == 401

    # senha curta é recusada pelo schema
    assert cliente.post(
        "/auth/trocar-senha",
        json={"senha_atual": "temporaria123", "senha_nova": "curta"},
        headers=cab,
    ).status_code == 422

    # troca válida
    assert cliente.post(
        "/auth/trocar-senha",
        json={"senha_atual": "temporaria123", "senha_nova": "SenhaNova2026"},
        headers=cab,
    ).status_code == 200

    # a antiga não serve mais; a nova serve e não pede troca
    assert cliente.post(
        "/auth/login",
        data={"username": "novato@cb.gov.br", "password": "temporaria123"},
    ).status_code == 401
    nova = cliente.post(
        "/auth/login",
        data={"username": "novato@cb.gov.br", "password": "SenhaNova2026"},
    )
    assert nova.status_code == 200
    assert nova.json()["precisa_trocar_senha"] is False


# --------------------------------------------------------------------------- #
# Cadastro de equipe — restrito a administrador e supervisor
# --------------------------------------------------------------------------- #


def test_bombeiro_nao_cadastra_ninguem(cliente):
    cab = _token(cliente, "b1@cb.gov.br")
    r = cliente.post(
        "/usuarios",
        json={"nome": "Intruso", "email": "intruso@cb.gov.br", "papel": "bombeiro"},
        headers=cab,
    )
    assert r.status_code == 403


def test_supervisor_cadastra_bombeiro_e_recebe_senha(cliente):
    cab = _token(cliente, "sup@cb.gov.br")
    r = cliente.post(
        "/usuarios",
        json={"nome": "Novo Bombeiro", "email": "NovoB@CB.gov.br", "papel": "bombeiro"},
        headers=cab,
    )
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert len(corpo["senha_inicial"]) >= 12
    assert corpo["email"] == "novob@cb.gov.br"  # normalizado para minúsculas

    # a senha devolvida funciona e exige troca
    login = cliente.post(
        "/auth/login",
        data={"username": "novob@cb.gov.br", "password": corpo["senha_inicial"]},
    )
    assert login.status_code == 200
    assert login.json()["precisa_trocar_senha"] is True


def test_supervisor_nao_cria_administrador(cliente):
    cab = _token(cliente, "sup@cb.gov.br")
    r = cliente.post(
        "/usuarios",
        json={
            "nome": "Tentativa",
            "email": "tentativa@cb.gov.br",
            "papel": "administrador",
        },
        headers=cab,
    )
    assert r.status_code == 403
    assert "administrador" in r.json()["detail"].lower()


def test_email_duplicado_e_recusado(cliente):
    cab = _token(cliente, "admin@cb.gov.br")
    dados = {"nome": "Repetido", "email": "repetido@cb.gov.br", "papel": "bombeiro"}
    assert cliente.post("/usuarios", json=dados, headers=cab).status_code == 200
    assert cliente.post("/usuarios", json=dados, headers=cab).status_code == 409


def test_desativar_preserva_historico(cliente):
    cab = _token(cliente, "admin@cb.gov.br")
    alvo = cliente.post(
        "/usuarios",
        json={"nome": "Sai Fora", "email": "saifora@cb.gov.br", "papel": "bombeiro"},
        headers=cab,
    ).json()

    r = cliente.patch(f"/usuarios/{alvo['id']}", json={"ativo": False}, headers=cab)
    assert r.status_code == 200
    assert r.json()["ativo"] is False

    # inativo não consegue mais entrar
    db = Sessao()
    usuario = db.get(m.Usuario, alvo["id"])
    db.close()
    assert usuario.ativo is False


def test_ninguem_desativa_a_propria_conta(cliente):
    cab = _token(cliente, "admin@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=cab).json()["id"]
    r = cliente.patch(f"/usuarios/{eu}", json={"ativo": False}, headers=cab)
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Indisponibilidades e preferências — a tela do bombeiro
# --------------------------------------------------------------------------- #


def test_bombeiro_registra_e_remove_as_proprias_datas(cliente):
    cab = _token(cliente, "b2@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=cab).json()["id"]

    r = cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": eu,
            "inicio": "2026-12-20",
            "fim": "2026-12-27",
            "tipo": "ferias",
        },
        headers=cab,
    )
    assert r.status_code == 200
    registro_id = r.json()["id"]

    # a listagem do bombeiro traz apenas as próprias
    lista = cliente.get("/indisponibilidades", headers=cab).json()
    assert all(i["bombeiro_id"] == eu for i in lista)
    assert any(i["id"] == registro_id for i in lista)

    assert cliente.delete(
        f"/indisponibilidades/{registro_id}", headers=cab
    ).status_code == 200
    assert not any(
        i["id"] == registro_id
        for i in cliente.get("/indisponibilidades", headers=cab).json()
    )


def test_bombeiro_nao_remove_registro_alheio(cliente):
    dono = _token(cliente, "b3@cb.gov.br")
    meu = cliente.get("/auth/eu", headers=dono).json()["id"]
    registro = cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": meu,
            "inicio": "2026-12-01",
            "fim": "2026-12-02",
            "tipo": "licenca",
        },
        headers=dono,
    ).json()["id"]

    outro = _token(cliente, "b4@cb.gov.br")
    assert cliente.delete(
        f"/indisponibilidades/{registro}", headers=outro
    ).status_code == 403


def test_data_final_anterior_a_inicial(cliente):
    cab = _token(cliente, "b2@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=cab).json()["id"]
    r = cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": eu,
            "inicio": "2026-12-20",
            "fim": "2026-12-10",
            "tipo": "ferias",
        },
        headers=cab,
    )
    assert r.status_code == 400


def test_indisponibilidade_avisa_plantao_publicado(cliente):
    """Caso 'Imprevistos': atestado sobre escala já publicada precisa avisar."""
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2026/9", headers=sup).json()
    alvo = next(p for p in escala["plantoes"] if p["data"] == "2026-09-24")

    r = cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": alvo["bombeiro_id"],
            "inicio": "2026-09-24",
            "fim": "2026-09-24",
            "tipo": "atestado",
        },
        headers=sup,
    )
    assert r.status_code == 200
    assert "2026-09-24" in r.json()["plantoes_em_conflito"]


def test_preferencia_exige_data_ou_dia_da_semana(cliente):
    cab = _token(cliente, "b2@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=cab).json()["id"]

    # nenhum dos dois
    assert cliente.post(
        "/preferencias", json={"bombeiro_id": eu, "tipo": "evita"}, headers=cab
    ).status_code == 400

    # os dois ao mesmo tempo
    assert cliente.post(
        "/preferencias",
        json={
            "bombeiro_id": eu,
            "tipo": "evita",
            "data": "2026-10-05",
            "dia_semana": 2,
        },
        headers=cab,
    ).status_code == 400

    # válido: dia da semana
    r = cliente.post(
        "/preferencias",
        json={"bombeiro_id": eu, "tipo": "evita", "dia_semana": 6},
        headers=cab,
    )
    assert r.status_code == 200
    pref_id = r.json()["id"]

    lista = cliente.get("/preferencias", headers=cab).json()
    assert any(p["id"] == pref_id and p["dia_semana"] == 6 for p in lista)
    assert cliente.delete(f"/preferencias/{pref_id}", headers=cab).status_code == 200


def test_supervisor_registra_data_de_outro_bombeiro(cliente):
    """Aviso que chega por telefone: o supervisor lança em nome do bombeiro."""
    sup = _token(cliente, "sup@cb.gov.br")
    bombeiros = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()
    alvo = bombeiros[2]["id"]

    r = cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": alvo,
            "inicio": "2027-01-05",
            "fim": "2027-01-09",
            "tipo": "atestado",
        },
        headers=sup,
    )
    assert r.status_code == 200

    # e consegue consultar filtrando por bombeiro
    lista = cliente.get(f"/indisponibilidades?bombeiro_id={alvo}", headers=sup).json()
    assert all(i["bombeiro_id"] == alvo for i in lista)


def test_escala_devolve_estagios_do_snapshot(cliente):
    """Regressão: o painel 'Como o motor decidiu' ficava vazio.

    Os estágios vinham só do resultado do job e eram perdidos ao recarregar.
    Agora saem do `solve_snapshots`, então navegar entre meses também mostra.
    """
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2026/9", headers=sup).json()

    assert escala["estagios"], "escala publicada deve devolver os estágios"
    assert len(escala["estagios"]) == 8  # E1..E6, com E5 em três partes
    primeiro = escala["estagios"][0]
    assert primeiro["codigo"] == "E1"
    assert primeiro["legivel"]
    assert escala["hash_entrada"]


def test_equidade_informa_quantas_escalas_publicadas(cliente):
    """Zero publicadas não é erro: é o estado inicial, e a tela explica isso."""
    sup = _token(cliente, "sup@cb.gov.br")

    # antes de qualquer publicação daquele ano
    antes = cliente.get("/equidade?ano=2020&mes=1", headers=sup).json()
    assert antes["escalas_publicadas"] == 0

    # depois: setembro/2026 foi publicado no fluxo completo
    depois = cliente.get("/equidade?ano=2026&mes=12", headers=sup).json()
    assert depois["escalas_publicadas"] >= 1
    assert any(
        abs(b["saldos"]["total"]) > 0 for b in depois["bombeiros"]
    ), "com escala publicada, algum saldo deve ser diferente de zero"


# --------------------------------------------------------------------------- #
# CRUD completo da equipe
# --------------------------------------------------------------------------- #


def test_editar_cadastro(cliente):
    cab = _token(cliente, "admin@cb.gov.br")
    criado = cliente.post(
        "/usuarios",
        json={"nome": "Nome Errado", "email": "errado@cb.gov.br", "papel": "bombeiro"},
        headers=cab,
    ).json()

    r = cliente.put(
        f"/usuarios/{criado['id']}",
        json={"nome": "Nome Certo", "email": "Certo@CB.gov.br"},
        headers=cab,
    )
    assert r.status_code == 200
    assert r.json()["nome"] == "Nome Certo"
    assert r.json()["email"] == "certo@cb.gov.br"  # normalizado


def test_editar_para_email_de_outro_e_recusado(cliente):
    cab = _token(cliente, "admin@cb.gov.br")
    a = cliente.post(
        "/usuarios",
        json={"nome": "Pessoa A", "email": "pessoa.a@cb.gov.br", "papel": "bombeiro"},
        headers=cab,
    ).json()
    cliente.post(
        "/usuarios",
        json={"nome": "Pessoa B", "email": "pessoa.b@cb.gov.br", "papel": "bombeiro"},
        headers=cab,
    )
    r = cliente.put(
        f"/usuarios/{a['id']}", json={"email": "pessoa.b@cb.gov.br"}, headers=cab
    )
    assert r.status_code == 409


def test_supervisor_nao_promove_ninguem(cliente):
    admin = _token(cliente, "admin@cb.gov.br")
    sup = _token(cliente, "sup@cb.gov.br")
    alvo = cliente.post(
        "/usuarios",
        json={"nome": "Ambicioso", "email": "ambicioso@cb.gov.br", "papel": "bombeiro"},
        headers=admin,
    ).json()
    r = cliente.put(
        f"/usuarios/{alvo['id']}", json={"papel": "administrador"}, headers=sup
    )
    assert r.status_code == 403


def test_redefinir_senha(cliente):
    cab = _token(cliente, "admin@cb.gov.br")
    alvo = cliente.post(
        "/usuarios",
        json={"nome": "Esquecido", "email": "esquecido@cb.gov.br", "papel": "bombeiro"},
        headers=cab,
    ).json()
    antiga = alvo["senha_inicial"]

    r = cliente.post(f"/usuarios/{alvo['id']}/redefinir-senha", headers=cab)
    assert r.status_code == 200
    nova = r.json()["senha_inicial"]
    assert nova != antiga

    entrada = cliente.post(
        "/auth/login", data={"username": "esquecido@cb.gov.br", "password": nova}
    )
    assert entrada.status_code == 200
    assert entrada.json()["precisa_trocar_senha"] is True
    assert cliente.post(
        "/auth/login", data={"username": "esquecido@cb.gov.br", "password": antiga}
    ).status_code == 401


def test_excluir_quem_nunca_trabalhou(cliente):
    cab = _token(cliente, "admin@cb.gov.br")
    alvo = cliente.post(
        "/usuarios",
        json={"nome": "Passageiro", "email": "passageiro@cb.gov.br", "papel": "bombeiro"},
        headers=cab,
    ).json()

    assert cliente.delete(f"/usuarios/{alvo['id']}", headers=cab).status_code == 200
    ids = [u["id"] for u in cliente.get("/usuarios", headers=cab).json()]
    assert alvo["id"] not in ids


def test_excluir_com_historico_e_recusado(cliente):
    """Apagar quem já trabalhou destruiria registro trabalhista."""
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2026/9", headers=sup).json()
    com_plantao = escala["plantoes"][0]["bombeiro_id"]

    r = cliente.delete(f"/usuarios/{com_plantao}", headers=sup)
    assert r.status_code == 409
    assert "Desativar" in r.json()["detail"]

    # desativar funciona e preserva a escala publicada
    assert cliente.patch(
        f"/usuarios/{com_plantao}", json={"ativo": False}, headers=sup
    ).status_code == 200
    assert len(cliente.get("/escalas/2026/9", headers=sup).json()["plantoes"]) == 30
    cliente.patch(f"/usuarios/{com_plantao}", json={"ativo": True}, headers=sup)


def test_ninguem_exclui_a_propria_conta(cliente):
    cab = _token(cliente, "admin@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=cab).json()["id"]
    assert cliente.delete(f"/usuarios/{eu}", headers=cab).status_code == 400


def test_bombeiro_nao_edita_nem_exclui(cliente):
    cab = _token(cliente, "b1@cb.gov.br")
    alvo = cliente.get("/usuarios?papel=bombeiro", headers=cab).json()[1]["id"]
    assert cliente.put(
        f"/usuarios/{alvo}", json={"nome": "Hackeado"}, headers=cab
    ).status_code == 403
    assert cliente.delete(f"/usuarios/{alvo}", headers=cab).status_code == 403
    assert cliente.post(
        f"/usuarios/{alvo}/redefinir-senha", headers=cab
    ).status_code == 403


# --------------------------------------------------------------------------- #
# Privacidade: o que o bombeiro vê e o que não vê
# --------------------------------------------------------------------------- #


def test_bombeiro_ve_escala_completa_mas_nao_motivo_de_ausencia(cliente):
    """A escala é operacional; o motivo da ausência é dado de saúde.

    Saber QUEM está de plantão é necessidade de trabalho: render turno,
    acionar em ocorrência, pedir troca. Saber POR QUE alguém faltou revela
    condição de saúde — dado pessoal sensível sob a LGPD — e não tem qualquer
    utilidade operacional para o colega.

    Este teste trava as duas metades: a escala continua visível a todos, e o
    motivo nunca vaza.
    """
    admin = _token(cliente, "admin@cb.gov.br")
    bombeiros = cliente.get("/usuarios?papel=bombeiro", headers=admin).json()
    outro = bombeiros[0]["id"]

    cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": outro,
            "inicio": "2027-03-02",
            "fim": "2027-03-06",
            "tipo": "atestado",
        },
        headers=admin,
    )

    espiao = _token(cliente, "b5@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=espiao).json()["id"]

    # não vê as indisponibilidades alheias, nem forçando o filtro
    for rota in ("/indisponibilidades", f"/indisponibilidades?bombeiro_id={outro}"):
        for registro in cliente.get(rota, headers=espiao).json():
            assert registro["bombeiro_id"] == eu, "vazou indisponibilidade de colega"

    for rota in ("/preferencias", f"/preferencias?bombeiro_id={outro}"):
        for registro in cliente.get(rota, headers=espiao).json():
            assert registro["bombeiro_id"] == eu, "vazou preferência de colega"

    # vê a escala inteira — necessidade operacional
    escala = cliente.get("/escalas/2026/9", headers=espiao).json()
    assert len(escala["plantoes"]) == 30
    assert len({p["bombeiro_id"] for p in escala["plantoes"]}) > 1

    # e nenhum campo do plantão carrega motivo de ausência
    campos = set().union(*(p.keys() for p in escala["plantoes"]))
    assert not campos & {
        "atestado", "licenca", "motivo_ausencia", "indisponibilidade",
        "tipo_indisponibilidade",
    }
    for p in escala["plantoes"]:
        texto = f"{p.get('observacoes', '')} {p.get('feriado') or ''}".lower()
        assert "atestado" not in texto
        assert "licença" not in texto

    # ferramentas de decisão do supervisor ficam fora do alcance
    assert cliente.get(
        "/escalas/2026/9/substitutos/10", headers=espiao
    ).status_code == 403
    assert cliente.get("/auditoria", headers=espiao).status_code == 403


def test_auditoria_funciona_em_sqlite(cliente):
    """Sem os triggers do Postgres, o registro sai pelo ORM.

    O aplicativo desktop usa SQLite; perder a auditoria em silêncio seria pior
    que não tê-la, porque a Parte 1 exige o registro.
    """
    cab = _token(cliente, "admin@cb.gov.br")
    criado = cliente.post(
        "/usuarios",
        json={
            "nome": "Auditado Silva",
            "email": "auditado@cb.gov.br",
            "papel": "bombeiro",
        },
        headers=cab,
    ).json()
    cliente.put(
        f"/usuarios/{criado['id']}", json={"nome": "Auditado Corrigido"}, headers=cab
    )

    registros = cliente.get("/auditoria?limite=200", headers=cab).json()
    do_alvo = [
        r for r in registros
        if r["entidade"] == "usuarios" and r["registro_id"] == str(criado["id"])
    ]
    operacoes = {r["operacao"] for r in do_alvo}
    assert "INSERT" in operacoes
    assert "UPDATE" in operacoes

    alteracao = next(r for r in do_alvo if r["operacao"] == "UPDATE")
    assert alteracao["antes"]["nome"] == "Auditado Silva"
    assert alteracao["depois"]["nome"] == "Auditado Corrigido"
    assert alteracao["motivo"], "toda alteração precisa registrar o motivo"


# --------------------------------------------------------------------------- #
# Rascunho x publicada
# --------------------------------------------------------------------------- #


def test_bombeiro_nao_ve_escala_em_rascunho(cliente):
    """Rascunho é escala que o supervisor ainda não aprovou.

    Pode mudar inteira ou ser descartada. Se o bombeiro a visse, planejaria a
    vida em cima de um plantão que talvez nunca exista — e a etapa de publicar,
    exigida na Parte 1, perderia o sentido.
    """
    sup = _token(cliente, "sup@cb.gov.br")
    bombeiro = _token(cliente, "b1@cb.gov.br")

    job = cliente.post(
        "/escalas/gerar", json={"ano": 2027, "mes": 4}, headers=sup
    ).json()["job_id"]
    resultado = cliente.get(f"/jobs/{job}", headers=sup).json()["resultado"]
    assert resultado["viavel"]

    # supervisor revisa
    rascunho = cliente.get("/escalas/2027/4", headers=sup)
    assert rascunho.status_code == 200
    assert rascunho.json()["status"] == "rascunho"

    # bombeiro não alcança por nenhuma via
    assert cliente.get("/escalas/2027/4", headers=bombeiro).status_code == 404
    assert cliente.get(
        "/escalas/2027/4/exportar/csv", headers=bombeiro
    ).status_code == 404
    assert cliente.get(
        "/escalas/2027/4/explicacao/10", headers=bombeiro
    ).status_code == 404
    assert cliente.get("/escalas/2027/4/versoes", headers=bombeiro).json() == []

    # depois de publicar, passa a ver
    cliente.post(f"/escalas/{rascunho.json()['id']}/publicar", headers=sup)
    publicada = cliente.get("/escalas/2027/4", headers=bombeiro)
    assert publicada.status_code == 200
    assert publicada.json()["status"] == "publicada"
    assert len(publicada.json()["plantoes"]) == 30


def test_regerar_nao_altera_o_que_o_bombeiro_ve(cliente):
    """O supervisor revisa a nova versão; o bombeiro segue na publicada."""
    sup = _token(cliente, "sup@cb.gov.br")
    bombeiro = _token(cliente, "b1@cb.gov.br")

    antes = cliente.get("/escalas/2027/4", headers=bombeiro).json()
    assert antes["status"] == "publicada"

    job = cliente.post(
        "/escalas/gerar", json={"ano": 2027, "mes": 4}, headers=sup
    ).json()["job_id"]
    cliente.get(f"/jobs/{job}", headers=sup)

    # supervisor vê o rascunho novo, para poder revisar antes de publicar
    visao_sup = cliente.get("/escalas/2027/4", headers=sup).json()
    assert visao_sup["status"] == "rascunho"
    assert visao_sup["versao"] > antes["versao"]

    # bombeiro continua exatamente onde estava
    depois = cliente.get("/escalas/2027/4", headers=bombeiro).json()
    assert depois["versao"] == antes["versao"]
    assert depois["status"] == "publicada"

    # e ao publicar, todo mundo avança junto
    cliente.post(f"/escalas/{visao_sup['id']}/publicar", headers=sup)
    final = cliente.get("/escalas/2027/4", headers=bombeiro).json()
    assert final["versao"] == visao_sup["versao"]

    versoes = cliente.get("/escalas/2027/4/versoes", headers=sup).json()
    assert sum(1 for v in versoes if v["status"] == "publicada") == 1


# --------------------------------------------------------------------------- #
# Achados da caçada a bugs
# --------------------------------------------------------------------------- #


def test_geracao_simultanea_do_mesmo_mes(cliente):
    """Duplo clique em 'Gerar' disparava duas gerações que competiam pelo mesmo
    número de versão; uma quebrava com violação de unicidade.

    O TestClient roda as tarefas de fundo de forma síncrona, então a corrida
    real não se reproduz aqui — ela foi verificada contra um servidor de
    verdade. O que este teste trava é a regra: com uma geração em andamento,
    a próxima do mesmo mês é recusada com 409.
    """
    sup = _token(cliente, "sup@cb.gov.br")

    db = Sessao()
    db.add(
        m.Job(
            id="job-em-andamento",
            tipo="gerar_escala:2028-03",
            status="executando",
            criado_em=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()
    db.close()

    bloqueada = cliente.post(
        "/escalas/gerar", json={"ano": 2028, "mes": 3}, headers=sup
    )
    assert bloqueada.status_code == 409
    assert "andamento" in bloqueada.json()["detail"]["mensagem"]

    # outro mês não é afetado
    assert cliente.post(
        "/escalas/gerar", json={"ano": 2028, "mes": 4}, headers=sup
    ).status_code == 200

    # concluído o job, o mês é liberado
    db = Sessao()
    job = db.get(m.Job, "job-em-andamento")
    job.status = "concluido"
    db.commit()
    db.close()
    assert cliente.post(
        "/escalas/gerar", json={"ano": 2028, "mes": 3}, headers=sup
    ).status_code == 200


def test_job_antigo_nao_bloqueia_para_sempre(cliente):
    """Job pendurado por processo morto travaria o mês indefinidamente."""
    from datetime import timedelta

    from api.main import LIMITE_JOB_MINUTOS

    sup = _token(cliente, "sup@cb.gov.br")
    db = Sessao()
    db.add(
        m.Job(
            id="job-fantasma",
            tipo="gerar_escala:2028-07",
            status="executando",
            criado_em=datetime.now(UTC).replace(tzinfo=None)
            - timedelta(minutes=LIMITE_JOB_MINUTOS + 5),
        )
    )
    db.commit()
    db.close()

    r = cliente.post("/escalas/gerar", json={"ano": 2028, "mes": 7}, headers=sup)
    assert r.status_code == 200, "job antigo bloqueou uma geração nova"


def test_ajustar_plantao_inexistente_devolve_404(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    r = cliente.patch("/plantoes/99999999", json={"novo_bombeiro_id": 1}, headers=sup)
    assert r.status_code == 404


def test_entradas_fora_da_faixa_sao_recusadas(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    for ano, mes in [(2026, 13), (2026, 0), (2026, -1), (1800, 6), (99999, 6)]:
        assert cliente.post(
            "/escalas/gerar", json={"ano": ano, "mes": mes}, headers=sup
        ).status_code == 422, f"aceitou {mes}/{ano}"


def test_indisponibilidade_com_periodo_absurdo(cliente):
    cab = _token(cliente, "b2@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=cab).json()["id"]
    r = cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": eu,
            "inicio": "2020-01-01",
            "fim": "2030-01-01",
            "tipo": "ferias",
        },
        headers=cab,
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Trocas — Parte 1: solicita -> aceita -> aprova -> valida -> atualiza
# --------------------------------------------------------------------------- #


#: Bombeiros criados pelo fixture `cliente`, cuja senha conhecemos. Testes
#: anteriores cadastram outras pessoas com senha gerada, que não serve aqui.
EMAILS_CONHECIDOS = {f"b{i}@cb.gov.br" for i in range(1, 8)}


@pytest.fixture(scope="module")
def escala_para_troca(cliente):
    """Escala futura publicada, para os testes de troca operarem sobre ela.

    Só entram os bombeiros do fixture: os cadastrados por outros testes têm
    senha aleatória e não daria para entrar como eles.
    """
    sup = _token(cliente, "sup@cb.gov.br")
    # desativa quem não é do grupo conhecido, para a escala usar só esses
    for u in cliente.get("/usuarios?papel=bombeiro", headers=sup).json():
        if u["email"] not in EMAILS_CONHECIDOS and u["ativo"]:
            cliente.patch(f"/usuarios/{u['id']}", json={"ativo": False}, headers=sup)
    job = cliente.post(
        "/escalas/gerar", json={"ano": 2029, "mes": 6}, headers=sup
    ).json()["job_id"]
    assert cliente.get(f"/jobs/{job}", headers=sup).json()["status"] == "concluido"
    escala = cliente.get("/escalas/2029/6", headers=sup).json()
    cliente.post(f"/escalas/{escala['id']}/publicar", headers=sup)
    return cliente.get("/escalas/2029/6", headers=sup).json()


def _par_permutavel(plantoes):
    """Acha dois plantões cuja permuta não cria plantão consecutivo.

    Escolher um par ao acaso costuma produzir uma troca inválida — o que é o
    comportamento certo do sistema, mas não serve para testar o caminho feliz.
    """
    from datetime import date as _date, timedelta as _td

    por_data = {_date.fromisoformat(p["data"]): p["bombeiro_id"] for p in plantoes}

    def sem_consecutivo(escala):
        por_pessoa = {}
        for dia, pessoa in escala.items():
            por_pessoa.setdefault(pessoa, []).append(dia)
        return all(
            all((b - a).days > 1 for a, b in zip(sorted(dias), sorted(dias)[1:]))
            for dias in por_pessoa.values()
        )

    for i, um in enumerate(plantoes):
        for outro in plantoes[i + 1:]:
            if um["bombeiro_id"] == outro["bombeiro_id"]:
                continue
            hipotese = dict(por_data)
            d1 = _date.fromisoformat(um["data"])
            d2 = _date.fromisoformat(outro["data"])
            hipotese[d1], hipotese[d2] = outro["bombeiro_id"], um["bombeiro_id"]
            if sem_consecutivo(hipotese):
                return um, outro
    raise AssertionError("nenhuma permuta válida encontrada nesta escala")


def _email_de(cliente, bombeiro_id):
    cab = _token(cliente, "sup@cb.gov.br")
    return next(
        u["email"] for u in cliente.get("/usuarios", headers=cab).json()
        if u["id"] == bombeiro_id
    )


def test_permuta_completa(cliente, escala_para_troca):
    """Fluxo inteiro: A pede, B aceita, supervisor aprova, escala muda."""
    sup = _token(cliente, "sup@cb.gov.br")
    plantoes = escala_para_troca["plantoes"]
    meu, dele = _par_permutavel(plantoes)

    a = _token(cliente, _email_de(cliente, meu["bombeiro_id"]))
    b = _token(cliente, _email_de(cliente, dele["bombeiro_id"]))

    pedido = cliente.post(
        "/trocas",
        json={
            "plantao_id": meu["id"],
            "plantao_oferecido_id": dele["id"],
            "motivo": "Compromisso familiar",
        },
        headers=a,
    )
    assert pedido.status_code == 200
    troca_id = pedido.json()["id"]

    aceite = cliente.post(f"/trocas/{troca_id}/aceitar", headers=b)
    assert aceite.status_code == 200
    assert aceite.json()["status"] == "aceita"

    aprovacao = cliente.post(f"/trocas/{troca_id}/aprovar", headers=sup)
    assert aprovacao.status_code == 200, aprovacao.text

    atual = cliente.get("/escalas/2029/6", headers=sup).json()
    por_id = {p["id"]: p for p in atual["plantoes"]}
    assert por_id[meu["id"]]["bombeiro_id"] == dele["bombeiro_id"]
    assert por_id[dele["id"]]["bombeiro_id"] == meu["bombeiro_id"]
    assert por_id[meu["id"]]["origem"] == "troca"
    assert len(atual["plantoes"]) == len(plantoes), "algum dia ficou descoberto"


def test_troca_que_viola_regra_e_bloqueada_na_aprovacao(cliente, escala_para_troca):
    """Os dois podem concordar e ainda assim a troca ser inválida.

    Assumir o dia seguinte ao próprio plantão cria dois turnos consecutivos.
    Nenhum dos bombeiros tem como perceber isso sozinho; o sistema recusa na
    aprovação e não altera nada.
    """
    sup = _token(cliente, "sup@cb.gov.br")
    atual = cliente.get("/escalas/2029/6", headers=sup).json()
    plantoes = sorted(atual["plantoes"], key=lambda p: p["data"])

    par = next(
        (a, b) for a, b in zip(plantoes, plantoes[1:])
        if a["bombeiro_id"] != b["bombeiro_id"]
    )
    vespera, seguinte = par

    dono = _token(cliente, _email_de(cliente, seguinte["bombeiro_id"]))
    vizinho = _token(cliente, _email_de(cliente, vespera["bombeiro_id"]))

    pedido = cliente.post(
        "/trocas", json={"plantao_id": seguinte["id"], "motivo": "Folga"}, headers=dono
    ).json()
    aceite = cliente.post(f"/trocas/{pedido['id']}/aceitar", headers=vizinho)
    assert aceite.status_code == 200
    assert aceite.json()["alerta"], "deveria avisar já no aceite"

    recusa = cliente.post(f"/trocas/{pedido['id']}/aprovar", headers=sup)
    assert recusa.status_code == 422
    assert "folga" in " ".join(recusa.json()["detail"]["violacoes"]).lower()

    depois = cliente.get("/escalas/2029/6", headers=sup).json()
    assert {p["id"]: p["bombeiro_id"] for p in depois["plantoes"]} == {
        p["id"]: p["bombeiro_id"] for p in atual["plantoes"]
    }, "a escala foi alterada apesar da recusa"


def test_regras_do_pedido(cliente, escala_para_troca):
    sup = _token(cliente, "sup@cb.gov.br")
    atual = cliente.get("/escalas/2029/6", headers=sup).json()
    meu = atual["plantoes"][20]
    outro = next(
        p for p in atual["plantoes"] if p["bombeiro_id"] != meu["bombeiro_id"]
    )
    dono = _token(cliente, _email_de(cliente, meu["bombeiro_id"]))

    # não dá para oferecer plantão alheio
    assert cliente.post(
        "/trocas", json={"plantao_id": outro["id"]}, headers=dono
    ).status_code == 403

    pedido = cliente.post("/trocas", json={"plantao_id": meu["id"]}, headers=dono)
    assert pedido.status_code == 200

    # nem abrir dois pedidos para o mesmo plantão
    assert cliente.post(
        "/trocas", json={"plantao_id": meu["id"]}, headers=dono
    ).status_code == 409

    # nem aceitar o próprio pedido
    assert cliente.post(
        f"/trocas/{pedido.json()['id']}/aceitar", headers=dono
    ).status_code == 400

    # o solicitante pode cancelar enquanto não foi aprovado
    cancelou = cliente.post(
        f"/trocas/{pedido.json()['id']}/recusar", json={"resposta": ""}, headers=dono
    )
    assert cancelou.status_code == 200
    assert cancelou.json()["status"] == "cancelada"


def test_nao_troca_plantao_de_rascunho(cliente):
    """Escala não publicada ainda pode ser regerada inteira."""
    sup = _token(cliente, "sup@cb.gov.br")
    job = cliente.post(
        "/escalas/gerar", json={"ano": 2029, "mes": 9}, headers=sup
    ).json()["job_id"]
    cliente.get(f"/jobs/{job}", headers=sup)
    rascunho = cliente.get("/escalas/2029/9", headers=sup).json()
    assert rascunho["status"] == "rascunho"

    alvo = rascunho["plantoes"][3]
    dono = _token(cliente, _email_de(cliente, alvo["bombeiro_id"]))
    r = cliente.post("/trocas", json={"plantao_id": alvo["id"]}, headers=dono)
    assert r.status_code == 409
    assert "publicada" in r.json()["detail"]


def test_bombeiro_nao_aprova_troca(cliente, escala_para_troca):
    sup = _token(cliente, "sup@cb.gov.br")
    atual = cliente.get("/escalas/2029/6", headers=sup).json()
    meu, dele = _par_permutavel(atual["plantoes"])
    dono = _token(cliente, _email_de(cliente, meu["bombeiro_id"]))
    aceitante = _token(cliente, _email_de(cliente, dele["bombeiro_id"]))

    pedido = cliente.post("/trocas", json={"plantao_id": meu["id"]}, headers=dono).json()
    cliente.post(f"/trocas/{pedido['id']}/aceitar", headers=aceitante)

    assert cliente.post(
        f"/trocas/{pedido['id']}/aprovar", headers=aceitante
    ).status_code == 403
    assert cliente.post(
        f"/trocas/{pedido['id']}/aprovar", headers=dono
    ).status_code == 403


def test_bombeiro_so_ve_trocas_que_lhe_dizem_respeito(cliente, escala_para_troca):
    sup = _token(cliente, "sup@cb.gov.br")
    todas = cliente.get("/trocas", headers=sup).json()
    assert todas, "o supervisor deveria ver todos os pedidos"

    espiao = _token(cliente, "b5@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=espiao).json()["id"]
    visiveis = cliente.get("/trocas", headers=espiao).json()

    for t in visiveis:
        envolvido = eu in (t["solicitante_id"], t["aceitante_id"])
        aberto = t["status"] == "solicitada" and t["aceitante_id"] is None
        assert envolvido or aberto, f"viu pedido alheio: {t['id']}"


# --------------------------------------------------------------------------- #
# Painel do dia e estatísticas
# --------------------------------------------------------------------------- #


def test_detalhe_do_dia_lista_candidatos(cliente, escala_para_troca):
    """O supervisor precisa saber POR QUE alguém não pode assumir, não só que
    não pode."""
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2029/6", headers=sup).json()
    alvo = escala["plantoes"][10]
    dia = int(alvo["data"][-2:])

    det = cliente.get(f"/escalas/2029/6/dia/{dia}", headers=sup)
    assert det.status_code == 200
    corpo = det.json()

    assert corpo["bombeiro_id"] == alvo["bombeiro_id"]
    assert corpo["candidatos"], "deveria sugerir substitutos"
    assert all(
        c["bombeiro_id"] != alvo["bombeiro_id"] for c in corpo["candidatos"]
    ), "quem já está escalado não é candidato a si mesmo"

    for c in corpo["candidatos"]:
        assert {"nome", "elegivel", "saldo_total", "nota"} <= set(c)
        if not c["elegivel"]:
            assert c["bloqueio"], "impedido sem motivo explicado"

    # impedidos vão para o fim
    elegiveis = [c["elegivel"] for c in corpo["candidatos"]]
    assert elegiveis == sorted(elegiveis, reverse=True)


def test_bombeiro_nao_recebe_lista_de_candidatos(cliente, escala_para_troca):
    """Escolher quem cobre é decisão do supervisor."""
    bombeiro = _token(cliente, "b1@cb.gov.br")
    corpo = cliente.get("/escalas/2029/6/dia/10", headers=bombeiro)
    if corpo.status_code == 200:
        assert corpo.json()["candidatos"] == []


def test_detalhe_de_data_invalida(cliente, escala_para_troca):
    sup = _token(cliente, "sup@cb.gov.br")
    assert cliente.get("/escalas/2029/6/dia/31", headers=sup).status_code == 422
    assert cliente.get("/escalas/2029/6/dia/99", headers=sup).status_code == 422


def test_estatisticas_do_ano(cliente, escala_para_troca):
    """A tela de Relatórios depende deste formato."""
    sup = _token(cliente, "sup@cb.gov.br")
    dados = cliente.get("/estatisticas?ano=2029", headers=sup).json()

    assert {"ano", "meses_publicados", "plantoes_totais", "ajustes_manuais",
            "preferencias", "trocas", "por_bombeiro"} <= set(dados)
    assert {"atendidas", "total", "percentual"} <= set(dados["preferencias"])
    assert {"aprovadas", "pendentes"} <= set(dados["trocas"])

    for linha in dados["por_bombeiro"]:
        assert {"nome", "total", "branca", "vermelha",
                "sabado", "domingo", "feriado"} <= set(linha)
        assert linha["branca"] + linha["vermelha"] == linha["total"]

    total = sum(l["total"] for l in dados["por_bombeiro"])
    assert total == dados["plantoes_totais"]

    # ano sem escala publicada devolve estrutura vazia, não erro
    vazio = cliente.get("/estatisticas?ano=1999", headers=sup).json()
    assert vazio["plantoes_totais"] == 0
    assert vazio["por_bombeiro"] == []
    assert vazio["preferencias"]["percentual"] is None


# --------------------------------------------------------------------------- #
# Modo exceção (Parte 0.5) e pendências
# --------------------------------------------------------------------------- #


def test_autorizar_excecao(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    alvo = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()[0]

    r = cliente.post(
        "/escalas/2030/3/excecoes",
        json={
            "data": "2030-03-15",
            "bombeiro_id": alvo["id"],
            "regra_dispensada": "H3",
            "justificativa": "Efetivo reduzido por surto de gripe",
        },
        headers=sup,
    )
    assert r.status_code == 200

    lista = cliente.get("/escalas/2030/3/excecoes", headers=sup).json()
    assert len(lista) == 1
    assert lista[0]["justificativa"].startswith("Efetivo reduzido")
    assert lista[0]["autorizada_por"]

    # repetir não duplica
    repetida = cliente.post(
        "/escalas/2030/3/excecoes",
        json={
            "data": "2030-03-15",
            "bombeiro_id": alvo["id"],
            "justificativa": "Efetivo reduzido por surto de gripe",
        },
        headers=sup,
    )
    assert repetida.json()["ja_existia"] is True


def test_excecao_exige_justificativa_e_data_do_mes(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    alvo = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()[0]

    curta = cliente.post(
        "/escalas/2030/4/excecoes",
        json={"data": "2030-04-10", "bombeiro_id": alvo["id"], "justificativa": "pq sim"},
        headers=sup,
    )
    assert curta.status_code == 422, "justificativa curta deveria ser recusada"

    fora = cliente.post(
        "/escalas/2030/4/excecoes",
        json={
            "data": "2030-05-10",
            "bombeiro_id": alvo["id"],
            "justificativa": "data de outro mês, não deveria passar",
        },
        headers=sup,
    )
    assert fora.status_code == 400


def test_ferias_nao_admitem_excecao(cliente):
    """Descanso é regra de operação; férias são direito da pessoa."""
    sup = _token(cliente, "sup@cb.gov.br")
    alvo = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()[1]

    cliente.post(
        "/indisponibilidades",
        json={
            "bombeiro_id": alvo["id"],
            "inicio": "2030-06-20",
            "fim": "2030-06-25",
            "tipo": "ferias",
        },
        headers=sup,
    )
    r = cliente.post(
        "/escalas/2030/6/excecoes",
        json={
            "data": "2030-06-22",
            "bombeiro_id": alvo["id"],
            "justificativa": "tentando forçar trabalho durante as férias",
        },
        headers=sup,
    )
    assert r.status_code == 422
    assert "férias" in r.json()["detail"].lower()


def test_bombeiro_nao_autoriza_excecao(cliente):
    bombeiro = _token(cliente, "b1@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=bombeiro).json()["id"]
    r = cliente.post(
        "/escalas/2030/7/excecoes",
        json={
            "data": "2030-07-10",
            "bombeiro_id": eu,
            "justificativa": "quero me liberar do descanso mínimo",
        },
        headers=bombeiro,
    )
    assert r.status_code == 403


def test_revogar_excecao(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    alvo = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()[2]
    criada = cliente.post(
        "/escalas/2030/8/excecoes",
        json={
            "data": "2030-08-05",
            "bombeiro_id": alvo["id"],
            "justificativa": "situação excepcional que depois se resolveu",
        },
        headers=sup,
    ).json()

    assert cliente.delete(f"/excecoes/{criada['id']}", headers=sup).status_code == 200
    assert cliente.get("/escalas/2030/8/excecoes", headers=sup).json() == []


def test_pendencias_por_papel(cliente, escala_para_troca):
    """Cada um vê o que depende dele."""
    sup = _token(cliente, "sup@cb.gov.br")
    bombeiro = _token(cliente, "b1@cb.gov.br")

    do_sup = cliente.get("/pendencias", headers=sup).json()
    do_bombeiro = cliente.get("/pendencias", headers=bombeiro).json()

    assert "itens" in do_sup and "total" in do_sup
    for item in do_sup["itens"]:
        assert {"tipo", "urgencia", "texto", "vista"} <= set(item)
        assert item["urgencia"] in ("alta", "media", "baixa")

    tipos_bombeiro = {i["tipo"] for i in do_bombeiro["itens"]}
    assert "escala_a_publicar" not in tipos_bombeiro, "publicar é do supervisor"
    assert "conflito_de_ausencia" not in tipos_bombeiro
    assert "troca_para_aprovar" not in tipos_bombeiro

    # mais urgente primeiro
    ordem = {"alta": 0, "media": 1, "baixa": 2}
    valores = [ordem[i["urgencia"]] for i in do_sup["itens"]]
    assert valores == sorted(valores)


# --------------------------------------------------------------------------- #
# Por que esta escala, e recomeçar do zero
# --------------------------------------------------------------------------- #


def test_porque_esta_escala(cliente, escala_para_troca):
    """O retrato das restrições que existiam quando o mês foi montado."""
    sup = _token(cliente, "sup@cb.gov.br")
    p = cliente.get("/escalas/2029/6/porque", headers=sup)
    assert p.status_code == 200
    corpo = p.json()

    esperados = {
        "dias_no_mes", "dias_vermelhos", "bombeiros_ativos", "ausencias",
        "entrada_do_mes", "preferencias", "dias_apertados", "estagios",
        "ajustes_manuais", "trocas_aplicadas", "feriados",
    }
    assert esperados <= set(corpo)
    assert corpo["dias_no_mes"] == 30
    assert 0 < corpo["dias_vermelhos"] < 30

    # entrada_do_mes cobre todo bombeiro ativo, com saldo e plantões do mês
    assert len(corpo["entrada_do_mes"]) == corpo["bombeiros_ativos"]
    for linha in corpo["entrada_do_mes"]:
        assert {"bombeiro", "saldo", "plantoes_no_mes"} <= set(linha)
    # ordenado do mais devedor para o mais credor
    saldos = [linha["saldo"] for linha in corpo["entrada_do_mes"]]
    assert saldos == sorted(saldos)

    # a soma dos plantões do mês fecha com os dias
    assert sum(l["plantoes_no_mes"] for l in corpo["entrada_do_mes"]) == 30

    pref = corpo["preferencias"]
    assert pref["atendidas"] <= pref["total"]
    for f in pref["frustradas"]:
        assert {"bombeiro", "data"} <= set(f)


def test_porque_respeita_rascunho(cliente):
    """Bombeiro não vê a explicação de escala que ainda não foi publicada."""
    sup = _token(cliente, "sup@cb.gov.br")
    bombeiro = _token(cliente, "b1@cb.gov.br")

    job = cliente.post(
        "/escalas/gerar", json={"ano": 2031, "mes": 5}, headers=sup
    ).json()["job_id"]
    cliente.get(f"/jobs/{job}", headers=sup)

    assert cliente.get("/escalas/2031/5/porque", headers=sup).status_code == 200
    assert cliente.get("/escalas/2031/5/porque", headers=bombeiro).status_code == 404


def test_recomecar_descarta_rascunho_e_ajustes(cliente):
    """Diferente de gerar de novo, que preserva o que foi travado."""
    sup = _token(cliente, "sup@cb.gov.br")

    job = cliente.post(
        "/escalas/gerar", json={"ano": 2031, "mes": 8}, headers=sup
    ).json()["job_id"]
    cliente.get(f"/jobs/{job}", headers=sup)
    escala = cliente.get("/escalas/2031/8", headers=sup).json()

    # trava um plantão ajustando à mão
    dia = int(escala["plantoes"][12]["data"][-2:])
    detalhe = cliente.get(f"/escalas/2031/8/dia/{dia}", headers=sup).json()
    livre = next(c for c in detalhe["candidatos"] if c["elegivel"])
    cliente.patch(
        f"/plantoes/{detalhe['plantao_id']}",
        json={"novo_bombeiro_id": livre["bombeiro_id"], "motivo": "teste"},
        headers=sup,
    )
    com_trava = cliente.get("/escalas/2031/8", headers=sup).json()
    assert any(p["travado"] for p in com_trava["plantoes"])

    r = cliente.delete("/escalas/2031/8/rascunho", headers=sup)
    assert r.status_code == 200
    assert r.json()["ajustes_perdidos"] >= 1
    assert cliente.get("/escalas/2031/8", headers=sup).status_code == 404


def test_recomecar_nao_toca_escala_publicada(cliente, escala_para_troca):
    """A equipe já se planejou com ela."""
    sup = _token(cliente, "sup@cb.gov.br")
    publicada = cliente.get("/escalas/2029/6", headers=sup).json()
    assert publicada["status"] == "publicada"

    r = cliente.delete("/escalas/2029/6/rascunho", headers=sup)
    assert r.status_code == 404
    assert cliente.get("/escalas/2029/6", headers=sup).json()["status"] == "publicada"


def test_bombeiro_nao_recomeca_escala(cliente):
    bombeiro = _token(cliente, "b1@cb.gov.br")
    assert cliente.delete(
        "/escalas/2031/9/rascunho", headers=bombeiro
    ).status_code == 403


# --------------------------------------------------------------------------- #
# Melhorias vindas do uso como escalante
# --------------------------------------------------------------------------- #


def test_nome_curto_e_telefone_no_cadastro(cliente):
    """No quartel ninguém usa nome completo, e o supervisor precisa ligar."""
    from api.servicos import derivar_nome_curto

    cab = _token(cliente, "admin@cb.gov.br")
    criado = cliente.post(
        "/usuarios",
        json={
            "nome": "Wagner Baldissera Fontanive",
            "email": "wagner.fontanive@cb.gov.br",
            "papel": "bombeiro",
            "nome_curto": "Sd. Fontanive",
            "telefone": "(49) 99999-1234",
        },
        headers=cab,
    )
    assert criado.status_code == 200

    lista = cliente.get("/usuarios", headers=cab).json()
    pessoa = next(u for u in lista if u["id"] == criado.json()["id"])
    assert pessoa["nome_curto"] == "Sd. Fontanive"
    assert pessoa["telefone"] == "(49) 99999-1234"

    # sem nome curto informado, o sistema deriva do nome completo
    sem = cliente.post(
        "/usuarios",
        json={
            "nome": "Iolanda Marcheti dos Passos",
            "email": "iolanda.passos@cb.gov.br",
            "papel": "bombeiro",
        },
        headers=cab,
    ).json()
    derivado = next(
        u for u in cliente.get("/usuarios", headers=cab).json() if u["id"] == sem["id"]
    )
    assert derivado["nome_curto"] == "Iolanda Passos"
    assert derivar_nome_curto("Ana Lu") == "Ana Lu"  # nome curto demais fica igual


def test_escala_devolve_nome_curto(cliente, escala_para_troca):
    """O calendário usa o nome curto; o completo fica no title."""
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2029/6", headers=sup).json()
    for p in escala["plantoes"]:
        assert p["bombeiro_curto"], "plantão sem nome curto"
        assert len(p["bombeiro_curto"]) <= len(p["bombeiro"])


def test_candidatos_trazem_telefone(cliente, escala_para_troca):
    """Sem o telefone aqui o supervisor sai do sistema para achar o contato."""
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2029/6", headers=sup).json()
    dia = int(escala["plantoes"][10]["data"][-2:])
    det = cliente.get(f"/escalas/2029/6/dia/{dia}", headers=sup).json()

    assert det["candidatos"]
    for c in det["candidatos"]:
        assert "telefone" in c, "candidato sem campo de telefone"


def test_porque_traz_distribuicao_do_mes(cliente, escala_para_troca):
    """Responde 'por que peguei três domingos' com os números daquele mês."""
    sup = _token(cliente, "sup@cb.gov.br")
    p = cliente.get("/escalas/2029/6/porque", headers=sup).json()

    assert "distribuicao" in p
    assert p["distribuicao"], "distribuição vazia"
    for linha in p["distribuicao"]:
        assert {"bombeiro", "total", "vermelha", "sabado", "domingo",
                "feriado"} <= set(linha)
        # sábados e domingos são subconjuntos dos dias vermelhos
        assert linha["sabado"] + linha["domingo"] <= linha["vermelha"] + linha["feriado"]

    assert sum(l["total"] for l in p["distribuicao"]) == p["dias_no_mes"]
    # ordenado por quem pegou mais fim de semana
    vermelhas = [l["vermelha"] for l in p["distribuicao"]]
    assert vermelhas == sorted(vermelhas, reverse=True)


def test_data_invalida_nao_derruba_o_servidor(cliente, escala_para_troca):
    """Regressão: `date(2029, 6, 99)` estourava e virava 500.

    A caçada pegou depois de uma mudança que reordenou a validação. Toda rota
    que monta data a partir da URL precisa validar antes de usar.
    """
    sup = _token(cliente, "sup@cb.gov.br")
    for rota in (
        "/escalas/2029/6/dia/{}",
        "/escalas/2029/6/explicacao/{}",
        "/escalas/2029/6/substitutos/{}",
    ):
        for dia in (0, 31, 99, 999):
            resposta = cliente.get(rota.format(dia), headers=sup)
            assert resposta.status_code != 500, (
                f"{rota.format(dia)} devolveu 500"
            )
            assert resposta.status_code in (404, 409, 422), (
                f"{rota.format(dia)} devolveu {resposta.status_code}"
            )


# --------------------------------------------------------------------------- #
# Achados do uso como supervisor
# --------------------------------------------------------------------------- #


def test_nome_curto_para_o_calendario(cliente):
    """Nome completo ocupa três linhas na célula e atrapalha bater o olho.

    A escala devolve as duas formas: a curta para o calendário, a completa
    para tabela, exportação e qualquer lugar que precise identificar sem
    ambiguidade.
    """
    sup = _token(cliente, "sup@cb.gov.br")
    escala = cliente.get("/escalas/2029/6", headers=sup)
    if escala.status_code != 200:
        pytest.skip("sem escala publicada neste mês")

    for p in escala.json()["plantoes"]:
        assert p["bombeiro"], "nome completo sumiu"
        assert p["bombeiro_curto"], "nome curto ausente"
        assert len(p["bombeiro_curto"]) <= len(p["bombeiro"])


def test_apelido_derivado_quando_nao_informado(cliente):
    """Sem apelido cadastrado, monta 'Primeiro Último' — nunca fica vazio."""
    from api.servicos import apelido

    class Falso:
        def __init__(self, nome, curto=""):
            self.nome, self.nome_curto = nome, curto

    assert apelido(Falso("Anderson Duarte Prates")) == "Anderson Prates"
    assert apelido(Falso("Maria Silva")) == "Maria Silva"
    assert apelido(Falso("Anderson Duarte Prates", "Sd. Prates")) == "Sd. Prates"
    assert apelido(Falso("Cristiane", "  ")) == "Cristiane"


def test_telefone_no_cadastro_e_nos_substitutos(cliente):
    """O supervisor precisa LIGAR para quem vai cobrir.

    Sem o telefone à mão, o sistema resolve a parte difícil — quem pode
    assumir — e para logo antes da parte fácil.
    """
    sup = _token(cliente, "sup@cb.gov.br")
    criado = cliente.post(
        "/usuarios",
        json={
            "nome": "Fulano de Contato",
            "nome_curto": "Sd. Contato",
            "telefone": "(49) 99999-1234",
            "email": "contato@cb.gov.br",
            "papel": "bombeiro",
        },
        headers=sup,
    )
    assert criado.status_code == 200

    lista = cliente.get("/usuarios", headers=sup).json()
    guardado = next(u for u in lista if u["id"] == criado.json()["id"])
    assert guardado["telefone"] == "(49) 99999-1234"
    assert guardado["nome_curto"] == "Sd. Contato"

    # e o painel do dia leva o telefone junto do candidato
    escala = cliente.get("/escalas/2029/6", headers=sup)
    if escala.status_code == 200:
        dia = int(escala.json()["plantoes"][8]["data"][-2:])
        detalhe = cliente.get(f"/escalas/2029/6/dia/{dia}", headers=sup).json()
        assert detalhe["candidatos"], "sem candidatos para conferir"
        assert all("telefone" in c for c in detalhe["candidatos"])


def test_editar_apelido_e_telefone(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    alvo = cliente.post(
        "/usuarios",
        json={"nome": "Antes Da Edicao", "email": "edicao@cb.gov.br",
              "papel": "bombeiro"},
        headers=sup,
    ).json()

    r = cliente.put(
        f"/usuarios/{alvo['id']}",
        json={"nome_curto": "Cb. Edicao", "telefone": "(49) 98888-7777"},
        headers=sup,
    )
    assert r.status_code == 200
    assert r.json()["nome_curto"] == "Cb. Edicao"
    assert r.json()["telefone"] == "(49) 98888-7777"


def test_extrato_responde_a_pergunta_da_reuniao(cliente, escala_para_troca):
    """"Por que peguei três domingos?" é sempre uma pergunta comparativa.

    O número sozinho não responde: precisa vir ao lado da média da equipe e do
    intervalo entre quem pegou menos e quem pegou mais.
    """
    sup = _token(cliente, "sup@cb.gov.br")
    alvo = cliente.get("/escalas/2029/6", headers=sup).json()["plantoes"][0]

    r = cliente.get(
        f"/bombeiros/{alvo['bombeiro_id']}/extrato?ano=2029", headers=sup
    )
    assert r.status_code == 200
    e = r.json()

    assert {"nome", "comparativo", "por_mes", "equipe_ativa"} <= set(e)
    categorias = {c["categoria"] for c in e["comparativo"]}
    assert categorias == {
        "total", "branca", "vermelha", "sabado", "domingo", "feriado"
    }

    for c in e["comparativo"]:
        assert {"meus", "media_da_equipe", "menor", "maior", "saldo"} <= set(c)
        assert c["menor"] <= c["maior"]
        assert c["menor"] <= c["meus"] <= c["maior"], (
            f"{c['categoria']}: o próprio valor deveria estar dentro do intervalo"
        )
        assert c["menor"] <= c["media_da_equipe"] <= c["maior"]

    # branca + vermelha fecham com o total
    por_cat = {c["categoria"]: c["meus"] for c in e["comparativo"]}
    assert por_cat["branca"] + por_cat["vermelha"] == por_cat["total"]

    # e o mês a mês soma o mesmo
    assert sum(l["total"] for l in e["por_mes"]) == por_cat["total"]


def test_bombeiro_so_ve_o_proprio_extrato(cliente, escala_para_troca):
    """Distribuição de plantões de colega é assunto do supervisor."""
    espiao = _token(cliente, "b5@cb.gov.br")
    eu = cliente.get("/auth/eu", headers=espiao).json()["id"]

    assert cliente.get(
        f"/bombeiros/{eu}/extrato?ano=2029", headers=espiao
    ).status_code == 200

    outro = next(
        u["id"] for u in cliente.get("/usuarios?papel=bombeiro", headers=espiao).json()
        if u["id"] != eu
    )
    assert cliente.get(
        f"/bombeiros/{outro}/extrato?ano=2029", headers=espiao
    ).status_code == 403


def test_extrato_de_ano_sem_escala(cliente):
    sup = _token(cliente, "sup@cb.gov.br")
    alvo = cliente.get("/usuarios?papel=bombeiro", headers=sup).json()[0]["id"]
    e = cliente.get(f"/bombeiros/{alvo}/extrato?ano=1999", headers=sup).json()
    assert e["meses_publicados"] == []
    assert all(c["meus"] == 0 for c in e["comparativo"])
