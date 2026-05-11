@echo off
REM ===================================================================
REM DahmerIA - Servidor publico (uma janela, um clique)
REM
REM Como usar:
REM   1. De duplo clique neste arquivo.
REM   2. Espere aparecer um link tipo "https://xxxxx.trycloudflare.com"
REM   3. Copie e mande pra sua namorada usar no celular ou PC dela.
REM
REM Enquanto esta janela e a janela "DahmerIA Backend" estiverem abertas,
REM o site fica online. Se voce fechar qualquer uma, o link morre.
REM ===================================================================

title DahmerIA - Servidor Publico
cd /d "%~dp0"

echo ============================================================
echo  DahmerIA - preparando servidor...
echo ============================================================
echo.

REM ---------- 1) Baixa o cloudflared.exe se ainda nao tiver ----------
if not exist "cloudflared.exe" (
    echo [1/3] Baixando cloudflared.exe pela primeira vez...
    curl -L -o cloudflared.exe "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    if errorlevel 1 (
        echo.
        echo ERRO: Nao consegui baixar o cloudflared automaticamente.
        echo Baixe manualmente em:
        echo   https://github.com/cloudflare/cloudflared/releases/latest
        echo Salve o arquivo como "cloudflared.exe" NESTA pasta e rode de novo.
        echo.
        pause
        exit /b 1
    )
    echo Download concluido!
    echo.
) else (
    echo [1/3] cloudflared.exe ja esta aqui.
    echo.
)

REM ---------- 2) Confere se o backend tem .venv configurado ----------
if not exist "backend\.venv\Scripts\activate.bat" (
    echo ERRO: O ambiente virtual Python nao foi encontrado em backend\.venv
    echo.
    echo Voce precisa configurar uma vez:
    echo   1. cd backend
    echo   2. python -m venv .venv
    echo   3. .venv\Scripts\activate
    echo   4. pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM ---------- 3) Sobe o backend numa janela separada ----------
echo [2/3] Iniciando backend numa nova janela...
start "DahmerIA Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\activate.bat && python main.py"

REM Espera 5s pro uvicorn subir antes de tentar tunelar
timeout /t 5 /nobreak >nul

REM ---------- 4) Sobe o tunel publico ----------
echo [3/3] Criando tunel publico...
echo.
echo ============================================================
echo  AGUARDE aparecer um link tipo:
echo    https://xxxxx-xxxxx-xxxxx.trycloudflare.com
echo.
echo  Copie esse link e mande pra ela. E so isso.
echo  NAO feche estas janelas enquanto ela estiver usando.
echo ============================================================
echo.

cloudflared.exe tunnel --url http://localhost:3001

REM Se cair aqui, o tunel foi fechado
echo.
echo Tunel encerrado.
pause
