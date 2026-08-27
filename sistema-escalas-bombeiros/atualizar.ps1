# atualizar.ps1 — traz a versão nova para C:\escalas e reinicia o sistema.
#
# Uso, a partir de C:\escalas:
#
#     .\atualizar.ps1              # atualiza o código, preserva os dados
#     .\atualizar.ps1 -Limpar      # atualiza e RECRIA o banco do zero
#
# Desde que o projeto usa migrações (Alembic), mudança de esquema NÃO exige
# mais apagar o banco: a aplicação atualiza as tabelas sozinha ao subir,
# preservando o que foi cadastrado.
#
# O -Limpar existe só para quando você quiser recomeçar do zero de propósito.

param(
    [switch]$Limpar,
    [string]$Origem = "$env:USERPROFILE\OneDrive - SENAC-SC\Documentos\bombeiros",
    [string]$Destino = "C:\escalas"
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "Procurando a versão mais recente em:" -ForegroundColor Cyan
Write-Host "  $Origem"

# O zip costuma extrair com uma pasta aninhada de mesmo nome. Em vez de supor
# a profundidade, procuramos o docker-compose.yml mais recente: onde ele está,
# está a raiz do projeto.
$marco = Get-ChildItem -Path $Origem -Filter "docker-compose.yml" -Recurse -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending |
         Select-Object -First 1

if (-not $marco) {
    Write-Host ""
    Write-Host "Nada encontrado." -ForegroundColor Red
    Write-Host "Extraia o zip em $Origem (botão direito no arquivo > Extrair Tudo) e rode de novo."
    exit 1
}

$pasta = $marco.DirectoryName
Write-Host "Encontrado ($($marco.LastWriteTime.ToString('dd/MM HH:mm'))):" -ForegroundColor Green
Write-Host "  $pasta"

# Copiar para fora do OneDrive: com "Arquivos Sob Demanda" ativo, o Docker às
# vezes recebe um atalho no lugar do arquivo e o build falha de forma confusa.
Write-Host ""
Write-Host "Copiando para $Destino..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Destino | Out-Null
Copy-Item -Path (Join-Path $pasta "*") -Destination $Destino -Recurse -Force

Set-Location $Destino

if ($Limpar) {
    Write-Host ""
    Write-Host "ATENÇÃO: isso apaga o banco e tudo que foi cadastrado." -ForegroundColor Yellow
    $resposta = Read-Host "Digite SIM para confirmar"
    if ($resposta -ne "SIM") {
        Write-Host "Cancelado. Nada foi apagado." -ForegroundColor Yellow
        exit 0
    }
    Write-Host "Removendo containers e volume..." -ForegroundColor Cyan
    docker compose down -v
}

Write-Host ""
Write-Host "Reconstruindo e subindo..." -ForegroundColor Cyan
docker compose up --build -d

Write-Host ""
Write-Host "Aguardando a API responder..." -ForegroundColor Cyan
$pronto = $false
foreach ($tentativa in 1..40) {
    Start-Sleep -Seconds 2
    try {
        Invoke-RestMethod "http://localhost:8000/saude" -TimeoutSec 3 | Out-Null
        $pronto = $true
        break
    } catch { }
}

Write-Host ""
if ($pronto) {
    Write-Host "  Pronto: http://localhost:8000" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Dê Ctrl+F5 na página para o navegador pegar a tela nova."
    if ($Limpar) {
        Write-Host "  Login: supervisor@cb.sc.gov.br  /  bombeiros2026"
    }
} else {
    Write-Host "  A API não respondeu a tempo. Veja o que aconteceu com:" -ForegroundColor Yellow
    Write-Host "     docker compose logs api --tail 40"
}
Write-Host ""
