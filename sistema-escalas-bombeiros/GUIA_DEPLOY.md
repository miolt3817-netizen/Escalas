# Publicar para o testador

Guia para colocar o sistema num endereço na internet, de forma que o testador
abra pelo navegador dele — sem instalar nada.

Tempo estimado: 20 a 30 minutos na primeira vez.

---

## Antes de começar: sobre dados reais

O sistema guarda escala de trabalho com nome, além de atestado e licença
médica. Isso é **dado pessoal sensível** sob a LGPD.

Recomendação para a avaliação: **use nomes fictícios**. O testador vai avaliar
se o sistema funciona, não se os dados estão certos — nomes reais não
acrescentam nada à avaliação e criam responsabilidade legal. Se ele quiser ver
a escala real dele, cadastra depois de entrar.

Se ainda assim for usar dados reais, saiba que o plano gratuito do Render:

- **não faz backup de nenhum tipo**;
- **apaga o banco 30 dias após a criação** (com 14 dias de tolerância para
  migrar para um plano pago).

---

## Passo 1 — Colocar o código no GitHub

O Render publica a partir de um repositório. Você já usa GitHub (`joaokuszera`).

1. Crie um repositório novo em <https://github.com/new>. Pode ser **privado** —
   o Render acessa repositórios privados normalmente.
2. Na pasta do projeto, no PowerShell:

```powershell
git init
git add .
git commit -m "Sistema de escalas para bombeiros"
git branch -M main
git remote add origin https://github.com/joaokuszera/escalas-bombeiros.git
git push -u origin main
```

(troque a URL pela do seu repositório)

O `.gitignore` já está no projeto, então banco local e cache não sobem.

---

## Passo 2 — Publicar no Render

1. Crie conta em <https://render.com> (dá para entrar com o GitHub). Não pede
   cartão de crédito para o plano gratuito.
2. No painel: **New** → **Blueprint**.
3. Escolha o repositório que você acabou de subir.
4. O Render lê o `render.yaml` do projeto e mostra o que vai criar: a API e o
   banco Postgres. Confirme.

O primeiro build leva de 5 a 10 minutos — o OR-Tools é um pacote grande.

O `render.yaml` já cuida do que costuma dar errado: gera um `JWT_SECRET` forte
e único, liga a API ao banco, e marca o ambiente como produção.

---

## Passo 3 — Pegar as senhas

Assim que o serviço subir, abra a aba **Logs** no painel do Render. Procure
por este bloco:

```
==================================================================
  SENHAS INICIAIS — anote agora, não são exibidas de novo.
  Cada usuário troca a senha no primeiro acesso.
==================================================================
  Administrador        admin@cb.sc.gov.br           4tHHF7dU3ZX9
  Sgt. Supervisor      supervisor@cb.sc.gov.br      NDRteNcU9wvc
  ...
```

Cada usuário recebe uma senha aleatória diferente. **Copie esse bloco agora** —
as senhas ficam guardadas apenas como hash e não são exibidas de novo.

O endereço do sistema aparece no topo da página do serviço, algo como
`https://escalas-api.onrender.com`.

---

## Passo 4 — Testar você mesmo antes de enviar

Abra o endereço, entre com o e-mail e a senha do supervisor. O sistema vai
pedir uma senha nova no primeiro acesso — defina uma e anote.

Depois: **Gerar escala** → **Publicar**. Se funcionou, está pronto para enviar.

---

## Passo 5 — O que mandar para o testador

Modelo de mensagem:

> Oi! O sistema de escalas está no ar, é só abrir pelo navegador — não precisa
> instalar nada.
>
> **Endereço:** https://escalas-api.onrender.com
> **E-mail:** supervisor@cb.sc.gov.br
> **Senha:** (a que você definiu)
>
> No primeiro acesso ele pede para você escolher uma senha nova.
>
> O que dá para fazer:
> 1. Clicar em **Gerar escala** — o sistema monta o mês inteiro sozinho, em
>    poucos segundos.
> 2. Clicar em **qualquer dia** do calendário — ele explica por que aquele
>    bombeiro foi escalado naquele dia.
> 3. Clicar em **Publicar** para fechar a escala.
> 4. Passar para o mês seguinte e gerar de novo — repare no painel "Saldo de
>    equidade", à direita: quem trabalhou menos em um mês recebe mais no
>    seguinte, automaticamente.
>
> Uma coisa: se ficar uns 15 minutos sem uso, a primeira página demora cerca de
> um minuto para abrir. É limitação do plano gratuito, não do sistema.

---

## Coisas que vão acontecer e são normais

**A primeira abertura demora ~1 minuto.** O plano gratuito do Render hiberna o
serviço após 15 minutos sem acesso. Avise o testador para não achar que travou.

**O banco expira em 30 dias.** Contado a partir da criação. O Render avisa por
e-mail. Depois disso há 14 dias para migrar para um plano pago (a partir de
US$ 6/mês) antes de os dados serem apagados.

**Esqueceu de copiar as senhas dos logs.** Recrie o banco: no painel, apague o
banco, rode o Blueprint de novo, e o seed gera senhas novas. Os dados
cadastrados se perdem.

---

## Se preferir não publicar na internet

Alternativa: rodar na sua máquina e deixar o testador acessar pela rede local
(mesma faculdade, mesmo Wi-Fi). Os dados não saem do seu computador.

1. Descubra seu IP local: `ipconfig` (procure "Endereço IPv4", algo como
   `192.168.0.15`)
2. Suba normalmente: `docker compose up`
3. O testador acessa `http://192.168.0.15:8000` do computador dele

Funciona apenas enquanto seu PC estiver ligado e os dois na mesma rede. Pode
ser preciso liberar a porta 8000 no Firewall do Windows.

---

## Segurança: o que mudou nesta versão

| Antes | Agora |
|---|---|
| `JWT_SECRET` com valor padrão previsível | Obrigatório em produção; o serviço nem inicia sem ele. Gerado automaticamente pelo Render |
| Todos os usuários com a mesma senha, escrita no código | Senha aleatória e diferente por usuário, exibida uma única vez |
| Sem troca de senha | Troca obrigatória no primeiro acesso, via `POST /auth/trocar-senha` |
| Login já preenchido na tela | Campos vazios |

O que **ainda falta** para uso além de avaliação: HTTPS já vem do Render, mas
não há limite de tentativas de login (proteção contra força bruta), nem
recuperação de senha por e-mail, nem backup automatizado.
