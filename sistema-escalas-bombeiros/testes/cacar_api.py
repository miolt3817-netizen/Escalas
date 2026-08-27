"""Caçada a bugs na API — script exploratório, não parte da suíte.

Roda contra um servidor de verdade em http://127.0.0.1:8877, com banco
recém-semeado. Diferente dos testes do pytest, este script existe para
procurar o que ninguém pensou: entrada malformada, ordem inesperada,
valores extremos e corrida entre pedidos.

    uvicorn api.main:app --port 8877
    python testes/cacar_api.py

O que ele encontra e é confirmado como bug vira teste no pytest.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

B = "http://127.0.0.1:8877"
achados: list[str] = []


def req(metodo, rota, corpo=None, token=None, form=False, cru=False, timeout=60):
    dados, cab = None, {}
    if corpo is not None:
        if form:
            dados = urllib.parse.urlencode(corpo).encode()
            cab["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            dados = json.dumps(corpo).encode()
            cab["Content-Type"] = "application/json"
    if token:
        cab["Authorization"] = "Bearer " + token
    pedido = urllib.request.Request(B + rota, data=dados, headers=cab, method=metodo)
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as r:
            bruto = r.read()
            return r.status, (bruto if cru else json.loads(bruto or b"{}"))
    except urllib.error.HTTPError as e:
        bruto = e.read()
        try:
            return e.code, (bruto if cru else json.loads(bruto or b"{}"))
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"erro_cliente": str(e)}


def checar(condicao, descricao, detalhe=""):
    if condicao:
        print(f"  ok    {descricao}")
    else:
        print(f"  FALHA {descricao}" + (f"  -> {detalhe}" if detalhe else ""))
        achados.append(f"{descricao} | {detalhe}")


def entrar(email, senha, nova):
    s, d = req("POST", "/auth/login", {"username": email, "password": senha}, form=True)
    if s != 200:
        s, d = req("POST", "/auth/login", {"username": email, "password": nova}, form=True)
        return d.get("access_token", "")
    t = d["access_token"]
    if d.get("precisa_trocar_senha"):
        req("POST", "/auth/trocar-senha", {"senha_atual": senha, "senha_nova": nova}, t)
        _, d = req("POST", "/auth/login", {"username": email, "password": nova}, form=True)
        t = d["access_token"]
    return t


SUP = entrar("supervisor@cb.sc.gov.br", "bombeiros2026", "SupHunt2026")
ADM = entrar("admin@cb.sc.gov.br", "bombeiros2026", "AdmHunt2026")
# O e-mail do bombeiro é descoberto pela API: fixá-lo quebrava o script a
# cada mudança na equipe semeada.
_, _equipe = req("GET", "/usuarios?papel=bombeiro", token=SUP)
_EMAIL_BOMBEIRO = next(u["email"] for u in _equipe if u["ativo"])
BOM = entrar(_EMAIL_BOMBEIRO, "bombeiros2026", "BombHunt2026")

print("\n=== 1. ENTRADAS FORA DA FAIXA ===")
for ano, mes, rotulo in [
    (2026, 13, "mês 13"), (2026, 0, "mês 0"), (2026, -1, "mês -1"),
    (1800, 6, "ano 1800"), (99999, 6, "ano 99999"),
]:
    s, _ = req("POST", "/escalas/gerar", {"ano": ano, "mes": mes}, SUP)
    checar(s == 422, f"gerar com {rotulo} é recusado", f"status {s}")

for rota in ["/escalas/2026/13", "/escalas/2026/0", "/escalas/abc/9"]:
    s, _ = req("GET", rota, token=SUP)
    checar(s in (404, 422), f"GET {rota} não quebra", f"status {s}")

s, _ = req("GET", "/escalas/2026/9/explicacao/99", token=SUP)
checar(s in (404, 422, 409), "explicação de dia 99 não quebra", f"status {s}")
s, _ = req("GET", "/escalas/2026/2/explicacao/30", token=SUP)
checar(s in (404, 422, 409), "explicação de 30/fev não quebra", f"status {s}")

print("\n=== 2. TEXTO MALICIOSO E EXTREMO ===")
s, d = req("POST", "/usuarios", {
    "nome": "<script>alert('xss')</script>", "email": "xss@cb.sc.gov.br",
    "papel": "bombeiro"}, ADM)
if s == 200:
    _, lista = req("GET", "/usuarios", token=ADM)
    guardado = next((u for u in lista if u["id"] == d["id"]), None)
    checar(guardado is not None, "nome com HTML é aceito e devolvido como dado")
    req("DELETE", f"/usuarios/{d['id']}", token=ADM)
else:
    checar(True, f"nome com HTML recusado (status {s})")

s, _ = req("POST", "/usuarios", {"nome": "A" * 500, "email": "longo@cb.sc.gov.br",
                                 "papel": "bombeiro"}, ADM)
checar(s == 422, "nome de 500 caracteres é recusado", f"status {s}")

import uuid as _uuid
sufixo = _uuid.uuid4().hex[:6]
s, criado = req("POST", "/usuarios", {"nome": "Sem Arroba",
                                      "email": f"naoehemail{sufixo}",
                                      "papel": "bombeiro"}, ADM)
checar(s in (200, 422), "e-mail sem arroba", f"status {s}")
if s == 200:
    req("DELETE", f"/usuarios/{criado['id']}", token=ADM)

s, _ = req("POST", "/usuarios", {"nome": "Perfil Falso", "email": "pf@cb.sc.gov.br",
                                 "papel": "superadmin"}, ADM)
checar(s == 422, "perfil inexistente é recusado", f"status {s}")

print("\n=== 3. INDISPONIBILIDADE COM DATAS ABSURDAS ===")
_, eu = req("GET", "/auth/eu", token=BOM)
casos = [
    ({"inicio": "2026-12-01", "fim": "2026-11-01"}, "fim antes do início", 400),
    ({"inicio": "2020-01-01", "fim": "2030-01-01"}, "período de 10 anos", 400),
    ({"inicio": "não-é-data", "fim": "2026-11-01"}, "data inválida", 422),
]
for extra, rotulo, esperado in casos:
    s, _ = req("POST", "/indisponibilidades",
               {"bombeiro_id": eu["id"], "tipo": "ferias", **extra}, BOM)
    checar(s == esperado, f"{rotulo} recusado", f"status {s} (esperado {esperado})")

s, _ = req("POST", "/indisponibilidades",
           {"bombeiro_id": eu["id"], "inicio": "2026-11-05", "fim": "2026-11-05",
            "tipo": "invencao"}, BOM)
checar(s == 422, "tipo de ausência inventado é recusado", f"status {s}")

print("\n=== 4. PERMISSÕES POR CAMINHO LATERAL ===")
s, _ = req("PUT", "/usuarios/1", {"papel": "administrador"}, BOM)
checar(s == 403, "bombeiro não se promove", f"status {s}")
s, _ = req("POST", "/parametros", {"chave": "intervalo_minimo_dias", "valor": "0"}, BOM)
checar(s in (403, 405), "bombeiro não altera parâmetros", f"status {s}")
s, _ = req("PUT", "/parametros", {"chave": "intervalo_minimo_dias", "valor": "0"}, SUP)
checar(s == 403, "supervisor não altera parâmetros", f"status {s}")
s, _ = req("GET", "/auditoria", token=BOM)
checar(s == 403, "bombeiro não lê auditoria", f"status {s}")

_, lista = req("GET", "/usuarios", token=SUP)
supervisores = [u for u in lista if u["papel"] == "supervisor"]
if supervisores:
    s, _ = req("PATCH", f"/usuarios/{supervisores[0]['id']}", {"ativo": False}, SUP)
    checar(s in (400, 403), "supervisor não desativa outro supervisor", f"status {s}")

print("\n=== 5. TOKEN ADULTERADO ===")
for ruim, rotulo in [
    ("", "vazio"), ("abc", "lixo"),
    (SUP[:-4] + "AAAA", "assinatura alterada"),
    (SUP + "x", "com sufixo"),
]:
    s, _ = req("GET", "/auth/eu", token=ruim)
    checar(s == 401, f"token {rotulo} recusado", f"status {s}")

print("\n=== 6. PUBLICAÇÃO E VERSÕES ===")
_, job = req("POST", "/escalas/gerar", {"ano": 2027, "mes": 3}, SUP)
for _ in range(60):
    time.sleep(1)
    _, j = req("GET", f"/jobs/{job['job_id']}", token=SUP)
    if j["status"] in ("concluido", "falhou"):
        break
checar(j["status"] == "concluido", "geração concluída", j.get("erro", "")[:120])

_, esc = req("GET", "/escalas/2027/3", token=SUP)
s1, _ = req("POST", f"/escalas/{esc['id']}/publicar", token=SUP)
s2, _ = req("POST", f"/escalas/{esc['id']}/publicar", token=SUP)
checar(s1 == 200 and s2 == 200, "publicar duas vezes não quebra", f"{s1}/{s2}")

_, versoes = req("GET", "/escalas/2027/3/versoes", token=SUP)
publicadas = sum(1 for v in versoes if v["status"] == "publicada")
checar(publicadas == 1, "apenas uma versão publicada por mês", f"{publicadas}")

s, _ = req("POST", "/escalas/999999/publicar", token=SUP)
checar(s == 404, "publicar escala inexistente devolve 404", f"status {s}")
s, _ = req("PATCH", "/plantoes/999999", {"novo_bombeiro_id": 1}, SUP)
checar(s == 404, "ajustar plantão inexistente devolve 404", f"status {s}")

print("\n=== 7. AJUSTE COM DADOS INVÁLIDOS ===")
_, esc = req("GET", "/escalas/2027/3", token=SUP)
alvo = esc["plantoes"][10]
s, _ = req("PATCH", f"/plantoes/{alvo['id']}", {"novo_bombeiro_id": 999999}, SUP)
checar(s in (422, 404), "ajustar para bombeiro inexistente é recusado", f"status {s}")
s, _ = req("PATCH", f"/plantoes/{alvo['id']}",
           {"novo_bombeiro_id": esc["plantoes"][9]["bombeiro_id"]}, SUP)
checar(s == 422, "ajuste que cria plantão consecutivo é recusado", f"status {s}")

print("\n=== 8. GERAÇÕES SIMULTÂNEAS ===")
respostas = [req("POST", "/escalas/gerar", {"ano": 2027, "mes": 6}, SUP)
             for _ in range(3)]
aceitas = [d["job_id"] for s, d in respostas if s == 200 and "job_id" in d]
recusadas = [s for s, _ in respostas if s == 409]
checar(len(aceitas) == 1, "só a primeira geração é aceita", str([s for s, _ in respostas]))
checar(len(recusadas) == 2, "as demais recebem 409 em vez de falhar", str(recusadas))

for _ in range(90):
    time.sleep(1)
    estados = [req("GET", f"/jobs/{i}", token=SUP)[1]["status"] for i in aceitas]
    if all(e in ("concluido", "falhou") for e in estados):
        break
checar(all(e == "concluido" for e in estados),
       "a geração aceita conclui sem erro", str(estados))

# depois de terminar, gerar de novo é permitido
s, d = req("POST", "/escalas/gerar", {"ano": 2027, "mes": 6}, SUP)
checar(s == 200, "nova geração liberada após a anterior terminar", f"status {s}")
if s == 200:
    for _ in range(60):
        time.sleep(1)
        e = req("GET", f"/jobs/{d['job_id']}", token=SUP)[1]
        if e["status"] in ("concluido", "falhou"):
            break
    checar(e["status"] == "concluido", "segunda geração conclui",
           e.get("erro", "")[:150])
_, versoes = req("GET", "/escalas/2027/6/versoes", token=SUP)
checar(len({v["versao"] for v in versoes}) == len(versoes),
       "sem versões duplicadas após corrida", str([v["versao"] for v in versoes]))

print("\n=== 9. EXPORTAÇÃO ===")
for formato, assinatura in [("pdf", b"%PDF"), ("xlsx", b"PK"), ("csv", b"\xef\xbb\xbf")]:
    s, b = req("GET", f"/escalas/2027/3/exportar/{formato}", token=SUP, cru=True)
    checar(s == 200 and b.startswith(assinatura), f"exportar {formato}", f"status {s}")
s, _ = req("GET", "/escalas/2027/3/exportar/exe", token=SUP)
checar(s == 400, "formato inventado recusado", f"status {s}")
s, _ = req("GET", "/escalas/2099/1/exportar/pdf", token=SUP)
checar(s == 404, "exportar mês sem escala devolve 404", f"status {s}")

print("\n=== 10. USUÁRIO DESATIVADO COM TOKEN VÁLIDO ===")
s, novo = req("POST", "/usuarios", {"nome": "Vai Sair", "email": "vaisair@cb.sc.gov.br",
                                    "papel": "bombeiro"}, ADM)
tok = entrar("vaisair@cb.sc.gov.br", novo["senha_inicial"], "VaiSair2026")
s, _ = req("GET", "/auth/eu", token=tok)
checar(s == 200, "usuário novo entra")
req("PATCH", f"/usuarios/{novo['id']}", {"ativo": False}, ADM)
s, _ = req("GET", "/auth/eu", token=tok)
checar(s == 401, "token de desativado para de valer imediatamente", f"status {s}")
req("DELETE", f"/usuarios/{novo['id']}", token=ADM)

print("\n=== 11. NAVEGAÇÃO PARA MESES DISTANTES ===")
for ano, mes in [(2026, 1), (2026, 12), (2027, 12), (2028, 2)]:
    s, _ = req("GET", f"/escalas/{ano}/{mes}", token=SUP)
    checar(s in (200, 404), f"consultar {mes:02d}/{ano}", f"status {s}")
s, eq = req("GET", "/equidade?ano=2030&mes=1", token=SUP)
checar(s == 200 and "bombeiros" in eq, "equidade em ano distante", f"status {s}")

print("\n" + "=" * 60)
if achados:
    print(f"{len(achados)} PROBLEMA(S) ENCONTRADO(S):")
    for a in achados:
        print(f"  - {a}")
else:
    print("NENHUM PROBLEMA ENCONTRADO")
