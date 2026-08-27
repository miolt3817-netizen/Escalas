# EscalaFogo — Sistema de escalas para bombeiros

Implementação da especificação v2. Assistente inteligente do supervisor: gera a
escala, explica suas decisões e permite ajustes antes da publicação.

**Estado atual**: Fases 1 a 3 do roadmap concluídas e testadas (modelo de dados,
API, motor completo com os seis estágios, equidade histórica, explicabilidade).
Fases 4 a 6 pendentes — ver "O que falta".

---

## Subir o projeto

```bash
docker compose up --build
```

Depois: <http://localhost:8000> (interface) e <http://localhost:8000/docs>
(documentação da API).

O seed cria as contas com **senha aleatória e diferente para cada usuário**,
exibidas uma única vez no terminal. Copie o bloco `SENHAS INICIAIS` que aparece
ao subir. Cada usuário troca a senha no primeiro acesso.

| Perfil | E-mail |
|---|---|
| Administrador | `admin@cb.sc.gov.br` |
| Supervisor | `supervisor@cb.sc.gov.br` |
| Bombeiros (8) | `joao@`, `maria@`, `carlos@`… `@cb.sc.gov.br` |

Para fixar uma senha só em desenvolvimento: `SENHA_INICIAL=minhasenha`.

**Publicar para alguém testar pelo navegador:** ver `GUIA_DEPLOY.md`.

### Variáveis de ambiente

| Variável | Padrão | Observação |
|---|---|---|
| `DATABASE_URL` | Postgres local | Aceita `postgres://` e `postgresql://`; o driver é normalizado |
| `JWT_SECRET` | inseguro em dev | **Obrigatório** quando `AMBIENTE=producao` — o serviço não inicia sem ele |
| `AMBIENTE` | `desenvolvimento` | `producao` ativa as exigências de segurança |
| `SENHA_INICIAL` | vazio | Se definido, todas as contas do seed usam essa senha (só para dev) |
| `PORT` | `8000` | Definido automaticamente por Render e Railway |

### Atualizar para uma versão nova (Windows)

O `atualizar.ps1` acompanha o projeto. A partir de `C:\escalas`:

```powershell
.\atualizar.ps1            # atualiza o código, preserva os dados
.\atualizar.ps1 -Limpar    # atualiza e recria o banco do zero
```

Ele procura a versão mais recente extraída em
`Documentos\bombeiros`, copia para fora do OneDrive (o modo "Arquivos Sob
Demanda" entrega atalhos no lugar dos arquivos e quebra o build), reconstrói a
imagem e espera a API responder.

O `-Limpar` só é necessário quando o modelo de dados muda — coluna ou tabela
nova. Ele apaga tudo que foi cadastrado, e pede confirmação antes.

Na primeira vez, o PowerShell pode recusar scripts. Libere para a sessão atual:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### Sem Docker

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://escalas:escalas@localhost:5432/escalas"
python -m api.seed                       # tabelas + init.sql + dados iniciais
uvicorn api.main:app --reload
```

`DATABASE_URL` aceita SQLite para testes locais, mas nele os triggers de
auditoria e a view materializada de equidade não existem — eles são
específicos do Postgres (`infra/init.sql`).

---

## Identidade visual

Marca **EscalaFogo**: capacete de bombeiro sobre calendário, com confirmação.
Vermelho `#d8181b` e carvão `#1c1d20`, amostrados do arquivo original.

**O cabeçalho é carvão, não vermelho** — decisão deliberada. Fundo vermelho
inteiro cansa num sistema usado horas por dia, e o vermelho precisa continuar
significando três coisas distintas ao mesmo tempo:

| Uso | Cor |
|---|---|
| Marca e ação primária | `#d8181b` |
| Escala vermelha (fim de semana e feriado) | tinta suave `#fdecec` |
| Erro e conflito | `#b3140f`, mais profundo, com ícone e barra |
| Aviso | laranja `#b45309`, da chama do símbolo |
| Sucesso | verde `#137a3d` |
| Seu plantão | carvão, para não competir com o vermelho |

O saldo de equidade usa laranja para quem está acima da parcela justa e carvão
para quem está abaixo. Nenhum dos dois é erro — vermelho ali alarmaria à toa.

A marca vive em `web/estatico/marca.svg`, em formas cheias para continuar
legível a 28px. Os ícones do aplicativo são gerados dela.

## Três formas de rodar

| Forma | Para quem | Banco | Como |
|---|---|---|---|
| **Aplicativo** | Supervisor no quartel | SQLite local | Dois cliques em `Escalas.exe` |
| **Docker** | Desenvolvimento | PostgreSQL | `docker compose up` |
| **Hospedado** | Acesso de qualquer lugar | PostgreSQL | Ver `GUIA_DEPLOY.md` |
| **Instalado (PWA)** | Celular e computador | — | "Instalar" no navegador |

O mesmo código roda nas três. O que muda é o banco e quem consegue alcançar.

### Aplicativo instalável (PWA)

A interface é um aplicativo instalável. No Android e no computador, o navegador
oferece "Instalar"; no iPhone, é Compartilhar → "Adicionar à Tela de Início". O
ícone vai para a tela inicial e abre em tela cheia, sem barra de navegador.

Isso entrega a experiência de aplicativo de loja **sem** conta de desenvolvedor,
taxa anual, ciclo de 12 testadores por 14 dias ou revisão. Requer HTTPS em
produção — o Render fornece; em desenvolvimento, `localhost` é aceito.

O service worker guarda **apenas a casca** (HTML, ícones, manifest). Nenhuma
resposta de API entra em cache: escala guardada no aparelho fica desatualizada
em silêncio, e alguém pode aparecer para trabalhar no dia errado. Além disso,
indisponibilidade envolve dado de saúde, que não deve sobrar no disco.

A interface também **não busca nada em servidor externo** — nem fontes. O
aplicativo instalado precisa funcionar sem internet, e buscar fonte numa CDN
enviaria o IP de cada usuário a um terceiro.

### Aplicativo desktop

```powershell
.\construir-exe.ps1        # gera dist\Escalas\ (5 a 10 min, uma vez só)
```

O PyInstaller **não faz compilação cruzada**: o executável do Windows precisa
ser gerado no Windows. O `escalas.spec` foi construído e validado em Linux —
motor CP-SAT, exportação e auditoria rodaram dentro do binário empacotado —
mas o build do Windows é o passo que roda na máquina de destino.

O que o aplicativo faz ao abrir: cria o banco em `%APPDATA%\EscalasBombeiros`,
gera um `JWT_SECRET` único e persistente na primeira execução, sobe o servidor,
abre o navegador e mostra o endereço da rede local para outros aparelhos do
quartel.

Duas adaptações foram necessárias para esse modo:

**PDF sem WeasyPrint.** O WeasyPrint depende de bibliotecas GTK que não existem
no Windows sem instalação separada. Quando ele falta, o PDF sai pelo ReportLab,
com o mesmo layout — `test_gerar_pdf_cai_no_reportlab_sem_weasyprint` garante
que a troca funciona.

**Auditoria sem triggers.** Os triggers de `infra/init.sql` são do PostgreSQL.
Em SQLite o registro é feito por eventos do SQLAlchemy: `before_flush` captura
o antes/depois enquanto o histórico existe, e `after_flush` grava, quando a
chave primária já foi atribuída. A limitação conhecida — não captura `UPDATE`
em massa nem SQL cru — não afeta o aplicativo, onde toda escrita passa pelo ORM.

## Estrutura

```
motor/            Motor de otimização — não conhece banco nem HTTP
  dominio.py        Tipos de entrada e saída
  calendario.py     Classificação branca/vermelha/sábado/domingo/feriado
  equidade.py       Saldo proporcional à disponibilidade
  modelo.py         Modelo CP-SAT: variáveis, H1–H4, assumptions
  estagios.py       Os seis estágios lexicográficos
  solver.py         Orquestração: viabilidade → estágios → verificação
  verificador.py    Validador independente das regras obrigatórias
  explicacao.py     Contrafactual, substitutos, resumo do assistente

api/              Backend FastAPI
  modelos.py        Tabelas SQLAlchemy
  servicos.py       Ponte banco ↔ motor, versionamento, ajustes
  seguranca.py      JWT + RBAC
  main.py           Rotas
  seed.py           Carga inicial

infra/init.sql    Triggers de auditoria + vw_equidade (Postgres)
web/index.html    Interface
desktop.py        Ponto de entrada do aplicativo
escalas.spec      Empacotamento PyInstaller
construir-exe.ps1 Build do executável (Windows)
atualizar.ps1     Atualização da instalação local
migracoes/        Migrações Alembic
testes/           111 testes + 2 scripts de caçada a bugs
```

---

## Como o motor funciona

### Restrições obrigatórias — hard, nunca pesos

```
H1  ∀d:  Σ_b x[b][d] = 1                    exatamente um por dia
H2  ∀b, d ∈ indisponível(b):  x[b][d] = 0   férias/licença/atestado/afastamento
H3  ∀b, d:  Σ janela x[b][·] ≤ 1            descanso mínimo, inclusive na virada do mês
H4  ∀(b,d) ∈ fixados:  x[b][d] = valor      dias travados e já decorridos
```

Se não houver solução, o sistema **não relaxa nada**. Devolve o conjunto mínimo
de restrições em conflito, obtido via `SufficientAssumptionsForInfeasibility()`:

```
Estas condições, em conjunto, tornam a cobertura impossível:
atestado do bombeiro 1 (2026-09-10 a 2026-09-10);
ferias do bombeiro 2 (2026-09-01 a 2026-09-30);
licenca do bombeiro 3 (2026-09-08 a 2026-09-12);
afastamento do bombeiro 4 (2026-09-09 a 2026-09-11).
```

### Seis estágios, não onze níveis

| | Objetivo |
|---|---|
| E1 | Espaçamento além do descanso obrigatório |
| E2 | Equalizar carga total **acumulada** |
| E3 | Equalizar escala branca **acumulada** |
| E4 | Equalizar escala vermelha **acumulada** |
| E5 | Sábados, domingos, feriados **acumulados** |
| E6 | Preferências |

Cada estágio é otimizado até o ótimo, travado com epsilon (`obj ≤ v + ε`) e
passado ao seguinte. Cobrir os dias e respeitar as obrigatórias são restrições,
não objetivos — por isso não geram estágio.

### Equidade proporcional

```
esperado[b][c] = total_dias[c] × (elegíveis[b][c] / Σ elegíveis[c])
saldo[b][c]    = realizado − esperado
```

Períodos de indisponibilidade não contam como dias elegíveis. Quem tira 30 dias
de férias volta com saldo **neutro**, não com déficit a ser compensado com
sobrecarga.

O saldo é sempre **derivado de `plantoes`** (view materializada no Postgres,
cálculo em Python como referência). Não existe tabela de contador — ela
dessincronizaria com trocas e ajustes feitos após a publicação.

### Explicabilidade

Contrafactual: para justificar "João no dia 12", resolve de novo com
`x[João][12] = 0` e compara.

| Resultado | Explicação |
|---|---|
| Infactível | "era a única opção que respeitava todas as restrições obrigatórias" |
| Pior em E*k* | "qualquer outra escolha pioraria \<critério\> (de X para Y)" |
| Equivalente | "havia alternativas igualmente boas; escolhido por desempate" |

A explicação e um snapshot das entradas (`solve_snapshots`) são gravados junto
com a escala — refazê-la meses depois, com dados diferentes, daria outra
resposta.

---

## Testes

```bash
pytest testes/ -q          # 27 testes, ~2 min
```

Cobertura, com o que cada grupo prova:

| Grupo | O que garante |
|---|---|
| Propriedades (Hypothesis, 40 cenários) | Para qualquer entrada: ou escala válida, ou infactível com diagnóstico. Nunca escala inválida |
| Fronteiras | Meses de 28/29/30/31 dias, virada de mês, efetivo mínimo, feriado em fim de semana |
| Infactibilidade | Nenhuma escala devolvida, conflito identificado |
| Verificador independente | Roda sobre toda saída do solver, escrito a partir da spec e não do modelo |
| Regressão de equidade | 12 meses encadeados |
| Controle | Sem compensação histórica, o desequilíbrio **acumula** |
| API | Login, RBAC, geração via job, publicação, versionamento, ajuste validado |
| Layout (navegador real) | Nenhuma tela transborda, corta conteúdo ou sobrepõe cabeçalho, em 1440px, 820px e 390px |
| Exportação | CSV com BOM e acentos corretos, XLSX com larguras definidas, PDF em página única |
| Privacidade | Bombeiro vê a escala inteira, mas nunca o motivo de ausência de colega |
| Rascunho x publicada | Bombeiro não alcança escala não publicada por nenhuma rota |
| Trocas | Fluxo completo, permuta que viola regra é bloqueada e a escala fica intacta |
| Concorrência | Geração simultânea do mesmo mês, job órfão que travava o mês |
| Sessão | F5, aba nova, expiração, token forjado |

### Caçada exploratória

Além do pytest, há dois scripts que tentam **quebrar** o sistema de propósito,
contra um servidor real:

```bash
uvicorn api.main:app --port 8877     # em outro terminal
python testes/cacar_api.py           # entradas absurdas, permissões, corrida
python testes/cacar_interface.py     # XSS, navegação, formulários, papéis
```

Foram eles que encontraram os quatro bugs corrigidos nesta versão: a corrida
de versão na geração simultânea, o job órfão que travava um mês para sempre,
o `422` onde deveria ser `404`, e o mês inicial fixo em setembro de 2026. Tudo
o que eles acham e se confirma vira teste no pytest.

Os testes automatizados rodam em SQLite, que **ignora triggers**. Os objetos de
`infra/init.sql` (auditoria e `vw_equidade`) foram validados manualmente contra
PostgreSQL 16 — ver "Validação em Postgres" abaixo. Automatizar essa parte com
um container de teste é dívida conhecida.

### Validação em Postgres

Conferido em PostgreSQL 16 com o fluxo completo: seed, geração, publicação.

| Verificação | Resultado |
|---|---|
| Triggers de auditoria | 30 plantões, 11 feriados, 10 usuários, 10 parâmetros e o `UPDATE` da publicação registrados |
| `vw_equidade` vs. cálculo em Python | Idênticos (+0,25 / −0,75 por bombeiro) |
| Refresh automático da view | `UPDATE` em `plantoes` atualiza o saldo sem intervenção |

### Resultado que justifica a correção nº 1 da especificação

Doze meses seguidos, 7 bombeiros, 365 dias:

| | Distribuição | Amplitude |
|---|---|---|
| Com compensação histórica | 52, 52, 52, 52, 53, 52, 52 | **1** |
| Sem compensação histórica | 50, 51, 52, 52, 53, 53, 54 | **4** |

365 ÷ 7 = 52,14 — amplitude 1 é o piso matemático, já que as atribuições são
inteiras. O teste `test_compensacao_historica_supera_o_controle` falha se
alguém reverter a correção.

---

## Decisões de domínio (Parte 0)

Configuradas em `parametros`, com os padrões recomendados. Confirmar com o
corpo de bombeiros antes de ir para produção:

| Parâmetro | Padrão | Pendência |
|---|---|---|
| `duracao_plantao_horas` | 24 | Confirmar o regime real (24×72? 24×48?) |
| `intervalo_minimo_dias` | 1 | Interpretação de "nunca 48h consecutivas" |
| `criterio_classificacao` | `inicio` | Plantão que cruza o dia é branca ou vermelha? |
| `intervalo_desejavel_dias` | 3 | Descanso desejável além do obrigatório |
| — | — | Válvula de escape quando infactível (0.5) |

Alterar pela API: `PUT /parametros` com `{"chave": "...", "valor": "..."}`.

---

## Perfis e o que cada um vê

| | Bombeiro | Supervisor | Administrador |
|---|---|---|---|
| Ver a escala | sim | sim | sim |
| Gerar e publicar | — | sim | sim |
| Ajustar plantão | — | sim | sim |
| Cadastrar bombeiros | — | sim | sim |
| Cadastrar supervisor/admin | — | — | sim |
| Editar cadastro | — | só bombeiros | todos |
| Redefinir senha de alguém | — | só bombeiros | todos |
| Ativar/desativar contas | — | só bombeiros | todos |
| Excluir cadastro | — | só bombeiros sem histórico | só quem não tem histórico |
| Informar indisponibilidade | só a própria | de qualquer um | de qualquer um |
| Informar preferências | só as próprias | de qualquer um | de qualquer um |
| Pedir troca | dos próprios plantões | — | — |
| Aceitar troca | sim | — | — |
| Aprovar troca | — | sim | sim |
| Ver datas de terceiros | — | sim | sim |
| Parâmetros do sistema | — | leitura | leitura e escrita |

A interface tem cinco abas, exibidas conforme o perfil: **Escala** (todos),
**Minhas datas** / **Datas da equipe** (todos), **Trocas** (todos, com selo de
pendências), **Equipe** e **Relatórios** (supervisor e administrador).

O botão **Ajuda** abre um guia de doze seções que se ajustam ao perfil. Além
de ensinar a operar, três seções explicam o funcionamento:

- **Como a escala é montada** — por que não é sorteio nem rodízio, e a
  diferença entre regra que nunca cede e critério que cede.
- **A ordem dos critérios** — os seis estágios em fila, e por que a fila é
  rígida em vez de soma de pontos.
- **O saldo de equidade** — o que o número significa, e por que férias não
  geram dívida.

Quem cadastra recebe a senha inicial gerada, exibida uma única vez, e a entrega
ao usuário — que escolhe a própria senha no primeiro acesso.

### Excluir x desativar

Quem **nunca entrou numa escala** pode ser excluído de vez — é o caso do
cadastro feito por engano.

Quem **já tem plantão registrado** não pode: apagar destruiria o histórico de
quem trabalhou quando, que é registro trabalhista. A API recusa com 409 e
explica o caminho: desativar. O bombeiro inativo sai das próximas escalas, mas
as publicadas continuam íntegras e o saldo de equidade histórico é preservado.

Não há recuperação de senha por e-mail. Quando alguém esquece, supervisor ou
administrador usa **Nova senha**: o sistema gera uma temporária, mostra uma
única vez, e a pessoa escolhe outra no acesso seguinte.

### Rascunho x publicada

Escala gerada nasce como **rascunho** e só o supervisor a enxerga. O bombeiro
vê apenas a **publicada** — por todas as vias: consulta, exportação, explicação
e lista de versões.

O motivo: rascunho é escala que ainda não foi aprovada. Pode mudar inteira ou
ser descartada. Se o bombeiro a visse, planejaria a vida em cima de um plantão
que talvez nunca exista, e a etapa de publicar perderia o sentido.

Regerar um mês já publicado cria uma versão nova em rascunho. O supervisor
passa a ver essa versão para revisar; o bombeiro continua na publicada até a
aprovação. Ao publicar, todos avançam juntos e a anterior vira `substituida`.

### Sessão

O token fica no `localStorage` e é **revalidado contra o servidor a cada
carregamento**. Recarregar a página não derruba a sessão; token vencido,
forjado ou de usuário desativado cai no login e a sessão local é apagada.
Se a sessão expirar durante o uso, a interface volta ao login em vez de falhar
em silêncio. O botão **Sair** encerra e limpa.

### O que o bombeiro vê

A escala **completa** fica visível para todos, com todos os nomes. É
necessidade operacional: saber quem vai render o turno, quem acionar numa
ocorrência, e com quem pedir troca. Também é o que sustenta a transparência
exigida na Parte 1 — sem enxergar a distribuição, ninguém consegue verificar
que ela é justa.

O **motivo da ausência**, não. Atestado e licença médica revelam condição de
saúde: dado pessoal sensível sob a LGPD, e sem qualquer utilidade operacional
para o colega. Cada bombeiro só enxerga as próprias indisponibilidades e
preferências, inclusive forçando o parâmetro `bombeiro_id` na URL. A escala
nunca carrega o motivo, e `test_bombeiro_ve_escala_completa_mas_nao_motivo_de_ausencia`
trava as duas metades.

Para o bombeiro achar os próprios dias rápido, os plantões dele aparecem
destacados no calendário e na tabela, com um filtro "Só os meus".

### Exportação

Três formatos, gerados **no servidor** — o navegador não controla largura de
coluna nem codificação de arquivo, e foi isso que produziu CSV ilegível no
Excel:

| Formato | Para quê |
|---|---|
| **PDF** | Mural do quartel. A4, mês inteiro em uma folha, resumo por bombeiro no rodapé |
| **XLSX** | Largura de coluna, cabeçalho fixo, autofiltro, cores por tipo e aba de resumo |
| **CSV** | Troca com outras ferramentas. UTF-8 **com BOM** e separador `;` |

O BOM não é detalhe: sem ele o Excel em português assume Windows-1252 e
"terça" vira "terÃ§a".

### Migrações

O esquema evolui com **Alembic**, não com `create_all`. A diferença importa:
`create_all` só cria tabelas que faltam e **nunca adiciona coluna a tabela que
já existe** — era por isso que toda atualização com mudança de modelo exigia
apagar o banco.

```bash
alembic revision --autogenerate -m "descrição"   # após mudar api/modelos.py
alembic upgrade head                              # aplicar
```

A aplicação roda `upgrade head` sozinha ao subir. Banco anterior ao Alembic é
adotado sem recriar nada: o sistema o carimba na versão atual e segue.

**Toda coluna com `default=` precisa também de `server_default=`.** Sem isso o
autogenerate emite `ALTER TABLE ... NOT NULL` sem valor, que o banco recusa em
tabela com linhas — e o erro só aparece em produção, onde há dados. O teste
`test_colunas_com_padrao_tem_server_default` percorre a árvore sintática dos
modelos e falha se alguém esquecer.

### Modo exceção (Parte 0.5)

Com efetivo pequeno e férias sobrepostas, pode não existir escala possível. O
sistema **nunca relaxa uma regra sozinho**: informa o conflito mínimo e oferece
ao supervisor autorizar uma exceção pontual.

A liberação vale para **um bombeiro, num dia, e mais nada** — o modelo CP-SAT
desativa só a janela de descanso que contém aquele par, e o verificador
independente reconhece a mesma autorização. Cada exceção exige justificativa de
pelo menos 10 caracteres e fica registrada com o nome de quem autorizou.

Só o descanso mínimo é dispensável. Cobrir todos os dias deixaria o quartel
vazio; férias e licença são direito da pessoa, e a API recusa com 422.

### Pendências

O sistema não envia e-mail nem push — ele é consultado. O painel **"Precisa da
sua atenção"** é o equivalente honesto: quem abre vê o que falta fazer, com um
botão que leva direto ao lugar de resolver.

| Aviso | Para quem |
|---|---|
| Troca aguardando aprovação | Supervisor |
| Troca aberta que você pode assumir | Bombeiro |
| Escala do mês seguinte não publicada, faltando ≤ 15 dias | Supervisor |
| Alguém de férias ou atestado ainda escalado em dia publicado | Supervisor |

### Por que esta escala

O botão **Por quê?**, na barra do mês, abre o retrato das condições que
existiam quando aquele mês foi montado — diferente da explicação de um dia, que
roda um contrafactual. Aqui não há cálculo novo: é a leitura do que estava
dado.

Mostra quem esteve fora e por quantos dias, com que saldo cada um entrou no
mês, quantas preferências foram atendidas e **quais não foram, com o dia**, os
dias em que havia pouca gente disponível, o resultado de cada critério, e
quantos plantões foram mexidos à mão depois.

Todo número vem dos plantões e cadastros. Nenhum é estimado.

### Recomeçar do zero

**Gerar escala** preserva os plantões travados por ajuste manual.
**Recomeçar** descarta o rascunho inteiro, junto com as travas, e deixa o motor
livre para redistribuir. O aviso diz quantos ajustes serão perdidos antes de
confirmar.

Escala publicada nunca é descartada — a equipe já se planejou com ela. Para
trocá-la, o caminho é gerar uma versão nova e publicar. O botão fica desativado
nesse caso, com a explicação no `title`.

### Painel do dia

Clicar num dia do calendário abre o que o supervisor usa no dia a dia: quem
está escalado, a explicação da escolha, e a lista de quem poderia assumir —
ordenada por saldo de equidade, a mesma prioridade do algoritmo.

Cada candidato traz o motivo em linguagem direta: *"está de férias nesta
data"*, *"trabalha em 27/08, sem o descanso mínimo"*. Saber **por que** não
pode importa tanto quanto saber que não pode. Quem tem preferência para aquele
dia aparece sinalizado.

Um clique escala a pessoa, com motivo obrigatório que vai para a auditoria. O
plantão fica travado: regerar o mês o preserva.

### Diálogos

`confirm()` e `prompt()` nativos foram substituídos por modais do sistema. Não
é só estética — o `prompt()` nativo aparece no celular com o endereço do site
em cima, e num sistema de trabalho isso passa impressão de improviso. Há teste
verificando que nenhum diálogo nativo voltou ao código.

### Trocas

Fluxo completo da Parte 1: **solicita → aceita → aprova → valida → atualiza**.

| Tipo | O que acontece |
|---|---|
| **Cessão** | O bombeiro passa um dia seu e não recebe nada em troca. Qualquer colega pode assumir. Muda um plantão |
| **Permuta** | Os dois trocam os dias entre si. O pedido já sai endereçado a quem detém o outro plantão. Muda dois plantões |

**A validação acontece na aprovação, não no pedido.** Entre um momento e outro
a escala pode ter mudado — outro ajuste, outra troca aprovada, uma
indisponibilidade nova. Validar só na criação deixaria passar troca que virou
inválida no meio do caminho.

E ela é indispensável: dois bombeiros podem concordar com uma troca que quebra
uma regra obrigatória sem que nenhum dos dois perceba. O caso típico é assumir
o dia seguinte ao próprio plantão, criando dois turnos consecutivos. O sistema
recusa na aprovação, explica qual regra seria quebrada — com o nome da pessoa,
não o identificador — e **não altera nada**.

A permuta muda dois dias de uma vez, então `validar_alteracoes` avalia as duas
mudanças **juntas**. Validar uma por vez daria resposta errada: o estado
intermediário, com a mesma pessoa nos dois dias, não é o que vai valer.

Só entram na troca plantões de escala **publicada** e que ainda **não
aconteceram**, e cada plantão admite um pedido em aberto por vez. O solicitante
pode cancelar enquanto não houver aprovação.

### Aviso de imprevisto

Ao registrar uma indisponibilidade sobre um período com escala já publicada, a
resposta traz `plantoes_em_conflito` com as datas afetadas, e a interface avisa
o supervisor de que aqueles dias precisam ser remanejados. É o caso
"Imprevistos" da Parte 1.

## Decisões técnicas que divergem do óbvio

**Sem Celery/Redis.** O solve leva 1–2 s. `BackgroundTasks` + polling em
`/jobs/{id}` resolve. Celery entra quando o solve passar de ~10 s, quando
entrar envio de e-mail/push, ou quando surgir agendamento recorrente — está no
backlog, não escondido como dívida.

**Auditoria por trigger, não por hook de ORM.** Hooks do SQLAlchemy não
capturam `UPDATE` em massa, SQL cru nem edição direta no banco. O campo
`motivo` é injetado por `SET LOCAL app.motivo`, lido pelo trigger.

**`passlib` substituído por `bcrypt` direto.** `passlib` está sem manutenção
desde 2020 e quebra com `bcrypt` 5.x.

**Determinismo obrigatório.** `random_seed` fixa e worker único: uma escala
precisa ser reproduzível para auditoria.

---

## O que falta

| Fase | Situação |
|---|---|
| 1. Base | Pronto |
| 2. Motor mínimo | Pronto |
| 3. Motor completo | Pronto |
| 4. Colaboração | Pronto — cadastro de equipe, gestão de datas, trocas e painel de pendências |
| 5. Inteligência visível | Explicações e estatísticas prontas; falta exportação Excel/PDF |
| 6. Polimento | Falta a interface React + TypeScript (`web/index.html` é página de verificação, não o produto) |

Ainda pendente: envio de e-mail ou push (hoje as pendências aparecem quando a
pessoa abre o sistema), e as decisões da Parte 0 — regime de plantão e
interpretação da regra das 48 h — que precisam ser confirmadas com o corpo de
bombeiros antes de ir para produção. Nenhuma linha de código resolve isso.
