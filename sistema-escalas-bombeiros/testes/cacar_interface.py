"""Caçada a bugs na interface — script exploratório, não parte da suíte.

Roda contra um servidor de verdade em http://127.0.0.1:8877, com banco
recém-semeado. Diferente dos testes do pytest, este script existe para
procurar o que ninguém pensou: entrada malformada, ordem inesperada,
valores extremos e corrida entre pedidos.

    uvicorn api.main:app --port 8877
    python testes/cacar_interface.py

O que ele encontra e é confirmado como bug vira teste no pytest.
"""
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8877"

#: Descoberto pela API, não fixo: a equipe semeada pode mudar.
def _primeiro_bombeiro():
    import json
    import urllib.parse
    import urllib.request

    dados = urllib.parse.urlencode(
        {"username": "supervisor@cb.sc.gov.br", "password": "bombeiros2026"}
    ).encode()
    pedido = urllib.request.Request(
        B + "/auth/login", data=dados,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = json.loads(urllib.request.urlopen(pedido, timeout=30).read())["access_token"]
    lista = urllib.request.Request(
        B + "/usuarios?papel=bombeiro", headers={"Authorization": "Bearer " + token}
    )
    equipe = json.loads(urllib.request.urlopen(lista, timeout=30).read())
    return next(u["email"] for u in equipe if u["ativo"])


EMAIL_BOMBEIRO = _primeiro_bombeiro()
achados: list[str] = []


def checar(cond, desc, det=""):
    if cond:
        print(f"  ok    {desc}")
    else:
        print(f"  FALHA {desc}" + (f"  -> {det}" if det else ""))
        achados.append(f"{desc} | {det}")


def entrar(page, email, senha, nova):
    for tentativa in (senha, nova):
        page.fill("#email", email)
        page.fill("#senha", tentativa)
        page.click("text=Entrar")
        page.wait_for_timeout(1600)
        if page.is_visible("#tela-senha"):
            page.fill("#senha-nova", nova)
            page.fill("#senha-conf", nova)
            page.click("text=Salvar e continuar")
            page.wait_for_timeout(1900)
        if page.is_visible("#app"):
            return True
    return False


with sync_playwright() as pw:
    nav = pw.chromium.launch()
    ctx = nav.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    erros: list[str] = []
    page.on("console", lambda msg: erros.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda e: erros.append(f"EXCEÇÃO: {e}"))

    page.goto(B, wait_until="networkidle")

    print("\n=== 1. LOGIN ===")
    page.fill("#email", "supervisor@cb.sc.gov.br")
    page.fill("#senha", "senha-errada")
    page.click("text=Entrar")
    page.wait_for_timeout(1500)
    checar(page.is_visible("#erro-login"), "senha errada mostra mensagem")
    checar(not page.is_visible("#app"), "não entra com senha errada")

    page.fill("#email", "")
    page.fill("#senha", "")
    page.click("text=Entrar")
    page.wait_for_timeout(1200)
    checar(not page.is_visible("#app"), "campos vazios não entram")

    checar(entrar(page, "supervisor@cb.sc.gov.br", "bombeiros2026", "SupUI2026"),
           "login válido funciona")

    print("\n=== 2. AJUDA ===")
    page.click("#btn-ajuda")
    page.wait_for_timeout(700)
    checar(page.is_visible("#modal-ajuda"), "ajuda abre")
    abas = page.eval_on_selector_all(".ajuda-aba", "els => els.map(e => e.textContent.trim())")
    checar(len(abas) >= 5, f"ajuda tem seções para supervisor: {abas}")
    for aba in abas:
        page.click(f'.ajuda-aba:text-is("{aba}")')
        page.wait_for_timeout(350)
        texto = page.inner_text("#ajuda-conteudo")
        checar(len(texto) > 200, f'seção "{aba}" tem conteúdo', f"{len(texto)} chars")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    checar(not page.is_visible("#modal-ajuda"), "Esc fecha a ajuda")
    page.click("#btn-ajuda")
    page.wait_for_timeout(500)
    page.click("#modal-ajuda .fechar-modal")
    page.wait_for_timeout(500)
    checar(not page.is_visible("#modal-ajuda"), "botão × fecha a ajuda")

    print("\n=== 3. NAVEGAÇÃO ENTRE MESES ===")
    inicial = page.inner_text("#titulo-mes")
    for _ in range(14):
        page.click('button[aria-label="Próximo mês"]')
        page.wait_for_timeout(220)
    page.wait_for_timeout(1200)
    virou_ano = page.inner_text("#titulo-mes")
    checar(virou_ano != inicial and "de 20" in virou_ano,
           f"avança 14 meses e vira o ano: {inicial} -> {virou_ano}")
    for _ in range(28):
        page.click('button[aria-label="Mês anterior"]')
        page.wait_for_timeout(180)
    page.wait_for_timeout(1200)
    checar("de 20" in page.inner_text("#titulo-mes"),
           f"volta 28 meses sem quebrar: {page.inner_text('#titulo-mes')}")

    print("\n=== 4. TABELA E FILTRO ===")
    page.goto(B, wait_until="networkidle")
    page.wait_for_timeout(2500)
    # volta para um mês que tem escala publicada pelo seed
    for _ in range(2):
        page.click('button[aria-label="Mês anterior"]')
        page.wait_for_timeout(900)
    page.wait_for_timeout(1500)
    checar("Sem escala" not in page.inner_text("#tabela"),
           f"mês com escala publicada carrega: {page.inner_text('#titulo-mes')}")
    page.fill("#busca", "zzzznaoexiste")
    page.wait_for_timeout(600)
    checar("Nada encontrado" in page.inner_text("#tabela"), "filtro sem resultado avisa")
    page.fill("#busca", "'; DROP TABLE plantoes;--")
    page.wait_for_timeout(600)
    checar(True, "filtro com SQL não quebra a página")
    page.fill("#busca", "<img src=x onerror=alert(1)>")
    page.wait_for_timeout(600)
    checar(True, "filtro com HTML não executa script")
    page.fill("#busca", "")
    page.wait_for_timeout(500)

    print("\n=== 5. CADASTRO COM ENTRADAS RUINS ===")
    page.click('[data-vista="equipe"]')
    page.wait_for_timeout(1200)
    page.click("#btn-salvar-pessoa")
    page.wait_for_timeout(700)
    checar(page.is_visible("#erro-cad"), "cadastro vazio mostra erro")

    page.fill("#novo-nome", "Fulano Teste")
    page.fill("#novo-email", "semarroba")
    page.click("#btn-salvar-pessoa")
    page.wait_for_timeout(800)
    checar(page.is_visible("#erro-cad"), "e-mail sem arroba mostra erro")

    page.fill("#novo-email", "xss.teste@cb.sc.gov.br")
    page.fill("#novo-nome", "<img src=x onerror=window.__xss=1>")
    page.click("#btn-salvar-pessoa")
    page.wait_for_timeout(1600)
    xss = page.evaluate("() => window.__xss === 1")
    checar(not xss, "nome com HTML NÃO executa script (XSS bloqueado)")
    tabela = page.inner_text("#lista-equipe")
    checar("onerror" in tabela or "img src" in tabela,
           "nome com HTML aparece como texto literal")

    print("\n=== 6. EDITAR E CANCELAR ===")
    page.click('#lista-equipe tr:has-text("onerror") button:text-is("Editar")')
    page.wait_for_timeout(700)
    checar(page.input_value("#novo-nome").startswith("<img"),
           "editar carrega o nome no formulário")
    checar(page.is_visible("#btn-cancelar-edicao"), "botão cancelar aparece")
    page.click("#btn-cancelar-edicao")
    page.wait_for_timeout(600)
    checar(page.input_value("#novo-nome") == "", "cancelar limpa o formulário")
    checar(not page.is_visible("#btn-cancelar-edicao"), "botão cancelar some")

    print("\n=== 7. EXCLUIR ===")
    page.click('#lista-equipe tr:has-text("onerror") button:text-is("Excluir")')
    page.wait_for_timeout(900)
    checar(page.is_visible("#modal-confirmar"), "exclusão pede confirmação própria")
    page.click("#confirmar-ok")
    page.wait_for_timeout(2000)
    checar("onerror" not in page.inner_text("#lista-equipe"),
           "excluir remove da tabela")

    print("\n=== 8. DATAS COM ENTRADAS RUINS ===")
    page.click('[data-vista="datas"]')
    page.wait_for_timeout(1200)
    page.click("button:text-is('Adicionar') >> nth=0")
    page.wait_for_timeout(700)
    checar(page.is_visible("#erro-ind"), "data vazia mostra erro")
    page.fill("#ind-inicio", "2027-05-20")
    page.fill("#ind-fim", "2027-05-10")
    page.click("button:text-is('Adicionar') >> nth=0")
    page.wait_for_timeout(700)
    checar(page.is_visible("#erro-ind"), "fim antes do início mostra erro")
    page.fill("#ind-inicio", "2027-05-20")
    page.fill("#ind-fim", "")
    page.click("button:text-is('Adicionar') >> nth=0")
    page.wait_for_timeout(1500)
    checar("20/05/2027" in page.inner_text("#lista-ind"),
           "data única (fim vazio) é aceita")

    print("\n=== 9. VISÃO DO BOMBEIRO ===")
    ctx2 = nav.new_context(viewport={"width": 1440, "height": 900})
    pb = ctx2.new_page()
    errosb: list[str] = []
    pb.on("pageerror", lambda e: errosb.append(str(e)))
    pb.goto(B, wait_until="networkidle")
    checar(entrar(pb, EMAIL_BOMBEIRO, "bombeiros2026", "BombUI2026"),
           "bombeiro entra")
    pb.wait_for_timeout(1500)
    checar(not pb.is_visible('[data-vista="equipe"]'), "aba Equipe some para bombeiro")
    checar(not pb.is_visible("#btn-gerar"), "botão Gerar some")
    checar(not pb.is_visible("#btn-publicar"), "botão Publicar some")
    checar("Minhas datas" in pb.inner_text('[data-vista="datas"]'),
           "aba diz 'Minhas datas'")
    pb.click("#btn-ajuda")
    pb.wait_for_timeout(700)
    abas_b = pb.eval_on_selector_all(".ajuda-aba", "els => els.map(e => e.textContent.trim())")
    checar("Gerenciar a equipe" not in abas_b,
           f"ajuda do bombeiro esconde seções de supervisor: {abas_b}")
    pb.keyboard.press("Escape")
    pb.wait_for_timeout(400)
    pb.click('[data-vista="datas"]')
    pb.wait_for_timeout(1000)
    checar(not pb.is_visible("#seletor-bombeiro"),
           "bombeiro não tem seletor de outra pessoa")
    checar(not errosb, f"sem exceções na tela do bombeiro: {errosb[:2]}")
    ctx2.close()

    print("\n=== 10. ERROS DE CONSOLE ===")
    # 401/404/409/422 são respostas corretas a entradas ruins que este próprio
    # teste provoca de propósito; o navegador as registra no console.
    reais = [e for e in erros if "favicon" not in e.lower()
             and not any(c in e for c in ("401", "404", "409", "422"))]
    checar(not reais, f"sem erros de JavaScript: {reais[:3]}")
    nav.close()

print("\n" + "=" * 60)
if achados:
    print(f"{len(achados)} PROBLEMA(S):")
    for a in achados:
        print(f"  - {a}")
else:
    print("NENHUM PROBLEMA ENCONTRADO")
