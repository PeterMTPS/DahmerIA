"""
services/ocr.py
----------------
OCR via Tesseract (binário do sistema) + pytesseract (wrapper Python).

Pré-requisito: o Tesseract precisa estar instalado no sistema.
  - Windows: https://github.com/UB-Mannheim/tesseract/wiki
  - Linux:   sudo apt install tesseract-ocr tesseract-ocr-por
  - macOS:   brew install tesseract tesseract-lang

Se o tesseract não estiver disponível, OCR_AVAILABLE fica False e o
backend simplesmente não oferece o modo OCR (cai pra um erro claro
explicando como instalar).
"""

import io
import logging
import os
import shutil
from pathlib import Path

from PIL import Image
import pytesseract

log = logging.getLogger("dahmeria.ocr")

# Caminhos onde o Tesseract aparece por default no Windows. Se o usuário
# não definir TESSERACT_CMD, a gente tenta achar nesses lugares antes de
# desistir — evita o "instale por favor" quando o cara JÁ instalou.
WINDOWS_DEFAULT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%USERPROFILE%\AppData\Local\Tesseract-OCR\tesseract.exe"),
]


def _resolve_tesseract_cmd() -> tuple[str | None, str]:
    """
    Descobre onde está o tesseract.exe.

    Ordem de tentativa:
      1. TESSERACT_CMD no .env (se preenchido)
      2. tesseract no PATH (shutil.which)
      3. Caminhos padrão do Windows

    Retorna (caminho_encontrado, descricao_da_origem).
    """
    # 1. Variável de ambiente
    env_cmd = os.getenv("TESSERACT_CMD", "").strip().strip('"').strip("'")
    if env_cmd:
        if Path(env_cmd).is_file():
            return env_cmd, f".env (TESSERACT_CMD={env_cmd})"
        log.warning("TESSERACT_CMD aponta pra %r, mas o arquivo não existe.", env_cmd)

    # 2. PATH do sistema
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path, f"PATH do sistema ({on_path})"

    # 3. Caminhos padrão do Windows
    for candidate in WINDOWS_DEFAULT_PATHS:
        if Path(candidate).is_file():
            return candidate, f"caminho padrão do Windows ({candidate})"

    return None, "não encontrado"


# -- Inicialização: descobre tesseract e checa se funciona --------------
_resolved_cmd, _resolution_origin = _resolve_tesseract_cmd()
if _resolved_cmd:
    pytesseract.pytesseract.tesseract_cmd = _resolved_cmd

try:
    _version = pytesseract.get_tesseract_version()
    OCR_AVAILABLE = True
    log.info("Tesseract %s encontrado via %s", _version, _resolution_origin)
except Exception as e:
    OCR_AVAILABLE = False
    _version = None
    log.warning("Tesseract NÃO disponível (%s): %s", _resolution_origin, e)


# Exporta info útil pra rota /health diagnosticar
TESSERACT_INFO = {
    "available": OCR_AVAILABLE,
    "command": _resolved_cmd,
    "origin": _resolution_origin,
    "version": str(_version) if _version else None,
}


def get_install_hint() -> str:
    """Mensagem de erro detalhada quando OCR é necessário mas não disponível."""
    lines = [
        "Tesseract OCR não está acessível.",
        f"Origem da busca: {_resolution_origin}",
        "",
        "Como resolver:",
        "  1) Instale o Tesseract:",
        "     - Windows: https://github.com/UB-Mannheim/tesseract/wiki",
        "       Durante a instalação, marque o idioma Portuguese.",
        "     - Linux:   sudo apt install tesseract-ocr tesseract-ocr-por",
        "     - macOS:   brew install tesseract tesseract-lang",
        "  2) No backend/.env, adicione (no Windows, ajuste o caminho se diferente):",
        r"       TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe",
        "     NÃO use aspas em volta do caminho.",
        "  3) REINICIE o backend (Ctrl+C e python main.py de novo).",
        "  4) Cheque se o caminho existe rodando no PowerShell:",
        r'       Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe"',
    ]
    if _resolved_cmd:
        lines.append("")
        lines.append(f"Caminho atualmente usado: {_resolved_cmd}")
        lines.append(
            "(Esse arquivo existe, mas o Tesseract retornou erro ao executar — "
            "talvez a instalação esteja corrompida; tente reinstalar.)"
        )
    return "\n".join(lines)


def extract_text(image_bytes: bytes, langs: str = "por+eng") -> str:
    """Extrai texto de uma imagem (em bytes) usando Tesseract."""
    if not OCR_AVAILABLE:
        raise RuntimeError(get_install_hint())

    img = Image.open(io.BytesIO(image_bytes))
    text = pytesseract.image_to_string(img, lang=langs)
    return text.strip()
