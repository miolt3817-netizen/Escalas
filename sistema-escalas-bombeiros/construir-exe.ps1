# construir-exe.ps1 — gera o aplicativo Windows a partir do código.
#
# Rode UMA vez na sua máquina; o resultado é uma pasta que você compacta e
# envia. Quem receber não precisa instalar nada.
#
#     .\construir-exe.ps1
#
# Requisito: Python 3.11 ou 3.12 instalado (python.org, marcando
# "Add python.exe to PATH" durante a instalação).
#
# O PyInstaller não faz compilação cruzada: o executável do Windows precisa
# ser gerado no Windows. Por isso este passo é seu, e não veio pronto.

param(
    [switch]$Limpar
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "=== Gerando o aplicativo Escalas ===" -ForegroundColor Cyan
Write-Host ""

# --- Python ---------------------------------------------------------------
try {
    $versao = (python --version 2>&1).ToString()
    Write-Host "Python: $versao" -ForegroundColor Green
} catch {
    Write-Host "Python não encontrado." -ForegroundColor Red
    Write-Host "Instale de https://www.python.org/downloads/ marcando"
    Write-Host "'Add python.exe to PATH' e abra um PowerShell novo."
    exit 1
}

# --- Ambiente isolado -----------------------------------------------------
if ($Limpar -and (Test-Path ".venv-build")) {
    Write-Host "Removendo ambiente anterior..." -ForegroundColor Yellow
    Remove-Item ".venv-build" -Recurse -Force
}

if (-not (Test-Path ".venv-build")) {
    Write-Host "Criando ambiente isolado (não mexe no seu Python)..." -ForegroundColor Cyan
    python -m venv .venv-build
}

$py = ".\.venv-build\Scripts\python.exe"

Write-Host "Instalando dependências (a primeira vez leva alguns minutos)..." -ForegroundColor Cyan
& $py -m pip install --upgrade pip --quiet

# WeasyPrint fica de fora: depende de GTK, que não existe no Windows sem
# instalação separada. O PDF sai pelo ReportLab — mesmo layout.
$pacotes = @(
    "ortools>=9.11,<10", "fastapi>=0.115", "uvicorn[standard]>=0.32",
    "sqlalchemy>=2.0", "pydantic>=2.9", "python-jose[cryptography]>=3.3",
    "bcrypt>=4.2", "python-multipart>=0.0.12", "openpyxl>=3.1",
    "reportlab>=4.2", "pyinstaller>=6.10"
)
& $py -m pip install --quiet @pacotes
if ($LASTEXITCODE -ne 0) {
    Write-Host "Falha ao instalar as dependências." -ForegroundColor Red
    exit 1
}

# --- Build ----------------------------------------------------------------
Write-Host ""
Write-Host "Empacotando (5 a 10 minutos — o OR-Tools é grande)..." -ForegroundColor Cyan
& $py -m PyInstaller escalas.spec --noconfirm --log-level WARN
if ($LASTEXITCODE -ne 0) {
    Write-Host "Falha ao empacotar." -ForegroundColor Red
    exit 1
}

$destino = Join-Path $PSScriptRoot "dist\Escalas"
if (-not (Test-Path (Join-Path $destino "Escalas.exe"))) {
    Write-Host "O executável não foi gerado. Confira a saída acima." -ForegroundColor Red
    exit 1
}

# --- Instruções para quem receber -----------------------------------------
$leiame = @"
ESCALAS — Corpo de Bombeiros
============================

COMO USAR
  Dê dois cliques em Escalas.exe.
  Uma janela abre com o endereço e o navegador abre sozinho.

PRIMEIRO ACESSO
  E-mail: supervisor@cb.sc.gov.br
  Senha:  bombeiros2026
  O sistema pede uma senha nova logo na entrada. Anote a que escolher.

OUTRAS PESSOAS NO MESMO WI-FI
  A janela mostra um segundo endereço, do tipo http://192.168.x.x:8000
  Quem estiver na mesma rede digita esse endereço no navegador do celular
  ou do computador e usa normalmente.
  Enquanto este computador estiver desligado, ninguém acessa.

ONDE FICAM OS DADOS
  %APPDATA%\EscalasBombeiros
  Para levar tudo para outro computador, copie essa pasta.
  Para começar do zero, apague o arquivo escalas.db de dentro dela.

AVISO DO WINDOWS
  Na primeira abertura pode aparecer "O Windows protegeu o computador".
  Clique em "Mais informações" e depois em "Executar assim mesmo".
  Isso acontece porque o programa não tem assinatura digital paga.

FIREWALL
  Se o Windows perguntar sobre acesso à rede, marque "Redes privadas".
  Sem isso, os outros aparelhos não conseguem acessar.
"@
$leiame | Out-File -FilePath (Join-Path $destino "LEIA-ME.txt") -Encoding UTF8

$tamanho = [math]::Round(
    ((Get-ChildItem $destino -Recurse | Measure-Object Length -Sum).Sum / 1MB), 0
)

Write-Host ""
Write-Host "=== Pronto ===" -ForegroundColor Green
Write-Host "  Pasta:  $destino"
Write-Host "  Tamanho: $tamanho MB"
Write-Host ""
Write-Host "Teste antes de enviar:" -ForegroundColor Cyan
Write-Host "  .\dist\Escalas\Escalas.exe"
Write-Host ""
Write-Host "Para enviar, compacte a pasta inteira:" -ForegroundColor Cyan
Write-Host "  Compress-Archive -Path '$destino' -DestinationPath Escalas.zip"
Write-Host ""
