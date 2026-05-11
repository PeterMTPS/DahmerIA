"""
main.py — DahmerIA backend (FastAPI)
=====================================

Sobe um servidor HTTP com:
  - POST /chat   -> recebe imagem (obrigatória) + dúvida opcional, devolve
                    a resolução da questão em Markdown
  - GET  /health -> diagnóstico (Ollama online? modelos? OCR disponível?)

E serve o frontend estático (../frontend) na mesma porta.

Roda com:
    uvicorn main:app --reload --port 3001
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Carrega variáveis do .env ANTES de importar serviços que usam env vars.
load_dotenv()

from services.gemini_svc import GeminiError, GeminiService
from services.image_proc import preprocess_image
from services.ocr import OCR_AVAILABLE, TESSERACT_INFO, extract_text, get_install_hint
from services.ollama_svc import OllamaError, OllamaService

# ---------------------------------------------------------------------------
# Configuração e logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dahmeria")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
TEXT_MODEL = os.getenv("OLLAMA_TEXT_MODEL", "llama3")
VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "llava")
PROCESSING_MODE = os.getenv("PROCESSING_MODE", "ocr").lower()
OCR_LANGS = os.getenv("OCR_LANGS", "por+eng")
PORT = int(os.getenv("PORT", "3001"))

# Provider default quando o frontend não manda nenhum:
#   - se Gemini está configurado, usa Gemini (lê imagem direto, qualidade melhor)
#   - senão, usa Ollama local
DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "auto").lower()

MAX_UPLOAD_MB = 10  # vai junto da validação manual do tamanho do upload

ollama = OllamaService(base_url=OLLAMA_URL)
gemini = GeminiService()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="DahmerIA — Assistente de Estudos")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Normaliza o formato dos erros para { ok: false, error: "..." }, que é o
# que nosso frontend já espera. Sem isso o FastAPI devolveria {"detail": ...}.
@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    log.exception("Erro inesperado: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": f"Erro inesperado no servidor: {exc}"},
    )


@app.on_event("startup")
async def _startup_banner():
    log.info("=" * 60)
    log.info(" DahmerIA - Assistente de Estudos")
    log.info("=" * 60)
    log.info(f" App:                http://localhost:{PORT}")
    log.info("")
    log.info(" Provider GEMINI (cloud):")
    log.info(f"   disponível:       {'SIM' if gemini.available else 'NÃO (configure GEMINI_API_KEY)'}")
    if gemini.available:
        log.info(f"   modelo:           {gemini.model_name}")
    log.info("")
    log.info(" Provider OLLAMA (local):")
    log.info(f"   url:              {OLLAMA_URL}")
    log.info(f"   modo:             {PROCESSING_MODE}")
    log.info(f"   modelo texto:     {TEXT_MODEL}")
    log.info(f"   modelo visão:     {VISION_MODEL}")
    log.info(f"   OCR Tesseract:    {'OK' if OCR_AVAILABLE else 'INDISPONÍVEL'}")
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Prompts (mantidos perto da rota pra ficar fácil de ajustar)
# ---------------------------------------------------------------------------
def build_vision_prompt(user_note: str, *, smart: bool = False) -> str:
    """Prompt para modelos multimodais.

    smart=True   -> Gemini (modelo grande, entende prompt detalhado em PT).
    smart=False  -> moondream/llava-7b (modelos pequenos, prompt curto em EN).
    """
    if smart:
        note = f"\n\nObservação do estudante: {user_note}" if user_note else ""
        return (
            "Você é um TUTOR de estudos paciente, didático e em português do Brasil.\n\n"
            "A imagem em anexo mostra uma QUESTÃO de prova/exercício "
            "(matemática, física, química, biologia, português, etc).\n\n"
            "Sua tarefa:\n\n"
            "**1. LEITURA**\n"
            "Leia TUDO na imagem com atenção: enunciado, fórmulas, gráficos, tabelas, "
            "alternativas. Se for múltipla escolha, transcreva todas as alternativas "
            "(a, b, c, d, e).\n\n"
            "**2. IDENTIFICAÇÃO**\n"
            'Identifique a disciplina e o tópico (ex.: "Matemática — Produtos notáveis").\n\n'
            "**3. RESOLUÇÃO PASSO A PASSO**\n"
            "Resolva mostrando o raciocínio:\n"
            '- Cada passo explica o "porquê", não só o "o quê"\n'
            "- Mostre as contas/manipulações algébricas\n"
            "- Use Markdown (negrito, listas)\n"
            "- IMPORTANTE: para fórmulas matemáticas use LaTeX SEM ENVOLVER EM "
            "BACKTICKS. Escreva $x^2 + 1$ inline e $$x = \\frac{-b \\pm \\sqrt{\\Delta}}{2a}$$ "
            "em destaque. NUNCA coloque fórmulas dentro de crases ou blocos de código — "
            "o site renderiza LaTeX automaticamente quando os $/$$ estão soltos no texto.\n\n"
            "**4. CONFERÊNCIA**\n"
            "Quando possível, verifique substituindo valores ou conferindo unidades.\n\n"
            "**5. RESPOSTA FINAL**\n"
            "Termine com uma seção '## Resposta final':\n"
            "- O resultado matemático/conceitual\n"
            "- Se for múltipla escolha, a LETRA correta\n\n"
            "Se a imagem estiver muito ilegível, diga isso CLARAMENTE em vez de "
            "adivinhar." + note
        )

    # Prompt curto pra modelos pequenos
    note = f"\nStudent note: {user_note}" if user_note else ""
    return (
        "Read the question in this image and answer in Portuguese (pt-BR).\n\n"
        "Tasks:\n"
        "1. Transcribe the question.\n"
        "2. Identify the subject (math, physics, etc).\n"
        "3. Solve step by step, explaining the reasoning.\n"
        "4. Use Markdown and LaTeX ($...$ inline, $$...$$ display).\n"
        "5. End with '## Resposta final' section." + note
    )


def build_chat_prompt(user_message: str) -> str:
    """Prompt para perguntas SEM imagem — conversa livre com o tutor."""
    return (
        "Você é um TUTOR de estudos paciente, didático e em português do Brasil. "
        "Responda à pergunta abaixo de forma clara e estruturada.\n\n"
        "DIRETRIZES:\n"
        "- Use Markdown (negrito, listas, títulos) pra organizar.\n"
        "- IMPORTANTE: pra fórmulas matemáticas use LaTeX SEM ENVOLVER EM "
        'BACKTICKS. Escreva $x^2 + 1$ inline e $$x = (-b ± √Δ)/2a$$ '
        "em destaque. NÃO escreva fórmulas dentro de blocos de código — "
        "o site renderiza LaTeX automaticamente.\n"
        "- Se for cálculo, mostre os passos.\n"
        "- Se for conceitual, explique com exemplos concretos.\n"
        "- Se a pergunta for ambígua, peça esclarecimento antes.\n"
        "- Termine com uma seção '## Resposta final' quando fizer sentido.\n\n"
        f"PERGUNTA DO ESTUDANTE:\n{user_message}"
    )


def build_text_prompt(question_text: str, user_note: str) -> str:
    """Prompt completo para modelos de texto puro (depois do OCR).

    O texto vem de OCR e pode ter erros típicos:
      - Expoentes virando dígitos normais (a² -> a2)
      - Símbolos matemáticos perdidos (√ desaparece, frações linearizam)
      - Caracteres trocados (l/I/1, O/0)
    O prompt explicita isso pra que o modelo INTERPRETE em vez de copiar literal.
    """
    note_block = f"\n\nObservação extra do estudante: {user_note}" if user_note else ""
    return (
        "Você é um TUTOR de matemática/ciências paciente, em português do Brasil.\n\n"
        "IMPORTANTE — O TEXTO ABAIXO VEIO DE UM OCR e pode ter erros:\n"
        "- Expoentes podem ter perdido a formatação (`a2` provavelmente é `a²`).\n"
        "- Raízes podem ter sumido (`xy` pode ser `√(xy)` se aparecer com `c =`).\n"
        "- Frações podem estar linearizadas (`x+y/2` provavelmente é `(x+y)/2`).\n"
        "- Caracteres podem estar trocados (`l`/`I`/`1`, `O`/`0`).\n"
        "INTERPRETE a notação pelo contexto matemático, não copie literal.\n\n"
        "PROCEDIMENTO OBRIGATÓRIO:\n\n"
        "**Passo A — Leitura limpa**\n"
        "Reescreva a questão com a notação matemática CORRIGIDA (usando LaTeX). "
        "Se for múltipla escolha (alternativas a, b, c, d, e), liste TODAS as alternativas "
        "que você identificou — mesmo que uma esteja incompleta, escreva o que faltou.\n\n"
        "**Passo B — Tema**\n"
        'Identifique a disciplina (ex.: "Matemática — Produtos notáveis").\n\n'
        "**Passo C — Resolução**\n"
        "Resolva PASSO A PASSO, explicando o porquê de cada manipulação algébrica. "
        "Mostre as contas, não só o resultado.\n\n"
        "**Passo D — Conferência**\n"
        "Substitua valores reais (ex.: x=4, y=1) nas fórmulas pra verificar se a relação "
        "que você achou bate. Mostre essa verificação.\n\n"
        "**Passo E — Resposta final**\n"
        'Termine com uma seção `## Resposta final` indicando claramente:\n'
        "- O resultado matemático\n"
        "- Se for múltipla escolha, A LETRA da alternativa correta\n\n"
        "REGRAS DE FORMATAÇÃO:\n"
        "- Markdown para títulos, listas, **negrito**.\n"
        "- LaTeX SEM envolver em crases/backticks: $a^2 - b^2$ inline e "
        "$$a^2 - b^2 = (a+b)(a-b)$$ em destaque. NUNCA coloque fórmulas dentro "
        "de blocos de código — o site renderiza LaTeX automaticamente.\n\n"
        "Se a questão estiver realmente ilegível a ponto de não dar pra interpretar, "
        "diga isso EXPLICITAMENTE em vez de inventar uma resposta.\n\n"
        "==== TEXTO BRUTO DO OCR ====\n"
        f"{question_text}\n"
        "==== FIM ===="
        f"{note_block}"
    )


# ---------------------------------------------------------------------------
# Helper: roda OCR + chamada de texto. Reusado no fluxo principal e no fallback.
# ---------------------------------------------------------------------------
async def run_ocr_pipeline(clean_image: bytes, user_message: str,
                           installed_models: list[str],
                           history: list[dict] | None = None) -> dict:
    if not OCR_AVAILABLE:
        raise HTTPException(503, get_install_hint())
    history = history or []

    if not _is_installed(TEXT_MODEL, installed_models):
        raise HTTPException(
            503,
            f'O modelo de texto "{TEXT_MODEL}" não está instalado. '
            f"Rode: ollama pull {TEXT_MODEL}\n\n"
            f"Modelos instalados: {', '.join(installed_models) or '(nenhum)'}.",
        )

    log.info("[ocr] extraindo texto via Tesseract (langs=%s)", OCR_LANGS)
    extracted = extract_text(clean_image, OCR_LANGS)

    # Loga o texto extraído INTEIRO no terminal — se a IA está dando resposta
    # ruim, normalmente é porque o OCR leu algo zoado. Aqui você vê na hora.
    log.info("[ocr] ===== TEXTO EXTRAÍDO (%d chars) =====", len(extracted))
    for line in extracted.splitlines():
        log.info("[ocr]   %s", line)
    log.info("[ocr] ===== FIM TEXTO =====")

    if not extracted or len(extracted) < 5:
        raise HTTPException(
            422,
            "Não consegui ler nenhum texto na imagem. Tente uma foto mais "
            "nítida, com boa iluminação e enquadrando bem a questão.",
        )

    log.info("[ocr] gerando resposta com %s (prompt: %d chars) | history=%d",
             TEXT_MODEL, len(extracted), len(history))
    base_prompt = build_text_prompt(extracted, user_message)
    final_prompt = _prepend_history_to_prompt(base_prompt, history)
    answer = await ollama.generate_text(model=TEXT_MODEL, prompt=final_prompt)
    return {
        "ok": True,
        "provider": "ollama",
        "mode": "ocr",
        "model": TEXT_MODEL,
        "extractedText": extracted,
        "answer": answer,
    }


def _is_installed(name: str, installed: list[str]) -> bool:
    """Match parcial: 'llava' bate em 'llava:latest', 'llava:13b'."""
    target = name.lower()
    return any(m.lower().startswith(target) for m in installed)


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Diagnóstico do backend — usado pelo frontend pra montar o seletor de provider."""
    models = await ollama.list_models()

    # Resolve qual seria o provider default agora (pra UI já vir selecionado certo).
    if DEFAULT_PROVIDER == "gemini":
        default_provider = "gemini" if gemini.available else "ollama"
    elif DEFAULT_PROVIDER == "ollama":
        default_provider = "ollama"
    else:  # auto
        default_provider = "gemini" if gemini.available else "ollama"

    return {
        "ok": True,
        "providers": {
            "gemini": gemini.info,
            "ollama": {
                "available": len(models) > 0,
                "url": OLLAMA_URL,
                "models": models,
                "textModel": TEXT_MODEL,
                "visionModel": VISION_MODEL,
                "mode": PROCESSING_MODE,
                "ocr_available": OCR_AVAILABLE,
            },
        },
        "defaultProvider": default_provider,
        "tesseract": TESSERACT_INFO,
    }


@app.post("/chat")
async def chat(
    image: UploadFile | None = File(None, description="Imagem (opcional)"),
    message: str = Form("", description="Pergunta/observação (opcional se houver imagem)"),
    provider: str = Form("", description='"gemini", "ollama" ou "" (default)'),
    history: str = Form("[]", description="JSON array de turnos anteriores"),
):
    user_message = (message or "").strip()
    chosen_provider = (provider or "").strip().lower()
    has_image = image is not None and image.filename

    # ---------- 1) Precisa ter algo ----------
    if not has_image and not user_message:
        raise HTTPException(
            400,
            "Envie uma imagem, escreva uma pergunta, ou os dois.",
        )

    # ---------- 2) Parse do histórico ----------
    # Formato esperado: [{role: "user"|"assistant", text: "..."}]
    # Se vier algo malformado, ignora e segue sem histórico.
    history_data: list[dict] = []
    try:
        parsed = json.loads(history) if history else []
        if isinstance(parsed, list):
            for turn in parsed:
                if isinstance(turn, dict) and "role" in turn and "text" in turn:
                    if turn["role"] in ("user", "assistant") and isinstance(turn["text"], str):
                        history_data.append({"role": turn["role"], "text": turn["text"]})
    except (json.JSONDecodeError, TypeError):
        log.warning("[chat] histórico malformado, ignorando")

    # ---------- 3) Resolve o provider efetivo ----------
    if not chosen_provider:
        chosen_provider = ("gemini" if gemini.available
                           else "ollama") if DEFAULT_PROVIDER == "auto" \
                          else DEFAULT_PROVIDER

    log.info("[chat] provider=%s | has_image=%s | history=%d turnos | msg=%r",
             chosen_provider, has_image, len(history_data), user_message[:60])

    # Log detalhado da memória pra diagnosticar quando a IA "esquece":
    if history_data:
        log.info("[chat] resumo do histórico recebido:")
        for i, turn in enumerate(history_data):
            preview = turn["text"][:80].replace("\n", " ")
            log.info("[chat]   #%d [%s] %s%s", i + 1, turn["role"], preview,
                     "..." if len(turn["text"]) > 80 else "")
    else:
        log.info("[chat] (sem histórico — primeira mensagem da conversa)")

    # ---------- 4) Caminho TEXT-ONLY (sem imagem) ----------
    if not has_image:
        if chosen_provider == "gemini":
            return await _handle_gemini_text(user_message, history_data)
        elif chosen_provider == "ollama":
            return await _handle_ollama_text(user_message, history_data)
        else:
            raise HTTPException(400, f'Provider desconhecido: "{chosen_provider}".')

    # ---------- 5) Caminho COM IMAGEM ----------
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(400, "O arquivo enviado não é uma imagem.")

    raw = await image.read()
    if len(raw) == 0:
        raise HTTPException(400, "Imagem vazia.")
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Imagem muito grande (máx. {MAX_UPLOAD_MB} MB).")

    try:
        clean_image = preprocess_image(raw)
    except ValueError as e:
        raise HTTPException(400, f"Imagem inválida: {e}")

    log.info("[chat] imagem: %d KB -> %d KB", len(raw) // 1024, len(clean_image) // 1024)

    if chosen_provider == "gemini":
        return await _handle_gemini(clean_image, user_message, history_data)
    elif chosen_provider == "ollama":
        return await _handle_ollama(clean_image, user_message, history_data)
    else:
        raise HTTPException(400, f'Provider desconhecido: "{chosen_provider}".')


def _prepend_history_to_prompt(prompt: str, history: list[dict]) -> str:
    """Pra Ollama: anexa o histórico como TEXTO antes do prompt principal.

    Mantém só os últimos 8 turnos pra não estourar contexto em modelos pequenos.
    """
    if not history:
        return prompt
    last = history[-8:]
    lines = ["Conversa anterior (lembre-se desse contexto):"]
    for turn in last:
        role = "Estudante" if turn["role"] == "user" else "Tutor"
        lines.append(f"{role}: {turn['text']}")
    lines.append("\nAgora responda à mensagem mais recente:\n")
    lines.append(prompt)
    return "\n".join(lines)


async def _handle_gemini_text(user_message: str, history: list[dict]) -> dict:
    """Pergunta de texto para o Gemini (sem imagem). Com memória."""
    if not gemini.available:
        raise HTTPException(
            503,
            'Provider "gemini" foi escolhido mas não está configurado.\n\n'
            "Adicione GEMINI_API_KEY no backend/.env "
            "(pegue gratuita em https://aistudio.google.com/apikey).",
        )

    # Prompt didático completo só na PRIMEIRA mensagem; depois é conversa natural.
    is_first = len(history) == 0
    prompt = build_chat_prompt(user_message) if is_first else user_message

    log.info("[gemini-text] history=%d", len(history))
    try:
        answer = await gemini.chat_text(prompt=prompt, history=history)
        return {
            "ok": True, "provider": "gemini", "model": gemini.model_name,
            "mode": "text", "extractedText": None, "answer": answer,
        }
    except GeminiError as e:
        detail = f"Gemini retornou erro: {e}"
        if e.hint:
            detail += f"\n\n💡 {e.hint}"
        raise HTTPException(status_code=502, detail=detail)


async def _handle_ollama_text(user_message: str, history: list[dict]) -> dict:
    """Pergunta de texto para o Ollama (sem imagem). Com memória."""
    installed_models = await ollama.list_models()
    if not installed_models:
        raise HTTPException(
            503,
            f"Não consegui me conectar ao Ollama em {OLLAMA_URL}. "
            'Verifique se está rodando ("ollama serve").',
        )
    if not _is_installed(TEXT_MODEL, installed_models):
        raise HTTPException(
            503,
            f'Modelo de texto "{TEXT_MODEL}" não instalado. '
            f"Rode: ollama pull {TEXT_MODEL}",
        )

    is_first = len(history) == 0
    base_prompt = build_chat_prompt(user_message) if is_first else user_message
    final_prompt = _prepend_history_to_prompt(base_prompt, history)

    log.info("[ollama-text] %s | history=%d", TEXT_MODEL, len(history))
    try:
        answer = await ollama.generate_text(model=TEXT_MODEL, prompt=final_prompt)
        return {
            "ok": True, "provider": "ollama", "model": TEXT_MODEL,
            "mode": "text", "extractedText": None, "answer": answer,
        }
    except OllamaError as e:
        detail = f'Ollama retornou erro: {e}'
        if e.hint:
            detail += f"\n\n💡 {e.hint}"
        raise HTTPException(status_code=502, detail=detail)


async def _handle_gemini(clean_image: bytes, user_message: str, history: list[dict]) -> dict:
    """Caminho do provider Gemini: imagem direto pra API, sem OCR. Com memória."""
    if not gemini.available:
        raise HTTPException(
            503,
            'Provider "gemini" foi escolhido mas não está configurado.\n\n'
            "Como configurar:\n"
            "  1) Pegue uma chave gratuita em https://aistudio.google.com/apikey\n"
            "  2) No backend/.env adicione: GEMINI_API_KEY=sua-chave-aqui\n"
            "  3) Reinicie o backend",
        )

    is_first = len(history) == 0
    prompt = (build_vision_prompt(user_message, smart=True) if is_first
              else (user_message or "Resolva esta nova questão na imagem."))

    log.info("[gemini] vision | %s | history=%d", gemini.model_name, len(history))
    try:
        answer = await gemini.chat_with_image(
            prompt=prompt,
            image_bytes=clean_image,
            history=history,
        )
        return {
            "ok": True, "provider": "gemini", "model": gemini.model_name,
            "mode": "vision", "extractedText": None, "answer": answer,
        }
    except GeminiError as e:
        detail = f"Gemini retornou erro: {e}"
        if e.hint:
            detail += f"\n\n💡 {e.hint}"
        raise HTTPException(status_code=502, detail=detail)


async def _handle_ollama(clean_image: bytes, user_message: str, history: list[dict]) -> dict:
    """Caminho do provider Ollama: vision local OU OCR + texto, conforme PROCESSING_MODE.

    Histórico é anexado como texto antes do prompt principal — modelos pequenos
    não têm chat session nativo no nosso wrapper.
    """
    # Confere Ollama online
    installed_models = await ollama.list_models()
    if not installed_models:
        raise HTTPException(
            503,
            f"Não consegui me conectar ao Ollama em {OLLAMA_URL}. "
            'Verifique se o Ollama está rodando ("ollama serve").',
        )

    # Decide a estratégia baseada em PROCESSING_MODE
    if PROCESSING_MODE == "vision":
        try_vision = True
    elif PROCESSING_MODE == "ocr":
        try_vision = False
    else:  # auto
        try_vision = _is_installed(VISION_MODEL, installed_models)

    # ---------- 5) Modo VISÃO ----------
    if try_vision:
        if not _is_installed(VISION_MODEL, installed_models):
            raise HTTPException(
                503,
                f'O modelo de visão "{VISION_MODEL}" não está instalado. '
                f"Rode: ollama pull {VISION_MODEL}\n\n"
                f"Modelos instalados: {', '.join(installed_models) or '(nenhum)'}.",
            )

        log.info("[vision] gerando resposta com %s | history=%d",
                 VISION_MODEL, len(history))
        try:
            base_prompt = build_vision_prompt(user_message)
            final_prompt = _prepend_history_to_prompt(base_prompt, history)
            answer = await ollama.generate_with_image(
                model=VISION_MODEL,
                prompt=final_prompt,
                image_bytes=clean_image,
            )

            # FALLBACK por QUALIDADE: modelos de visão pequenos costumam falhar
            # de duas formas previsíveis:
            #   1. Resposta CURTA (< 120 chars) — geralmente um trecho aleatório.
            #   2. Resposta LONGA mas que ADMITE que não leu a imagem
            #      (frases tipo "preciso de mais contexto", "texto fragmentário",
            #       "não é possível determinar"). Essas são as piores porque
            #       parecem resposta de verdade mas são alucinação.
            # Em ambos os casos, OCR + llama3 8B costuma resolver muito melhor.
            answer_lower = answer.lower()
            hallucination_signals = [
                "mais contexto",
                "mais informações",
                "fragmentári",         # fragmentário/fragmentária
                "ilegível",
                "incompleta",
                "não é possível determinar",
                "não consigo identificar",
                "não consegui ler",
                "ficarei feliz em ajudar",
                "se você puder fornecer",
                "could not read",
                "i cannot read",
                "unable to read",
                "not enough information",
            ]
            saw_hallucination = any(sig in answer_lower for sig in hallucination_signals)
            no_structure = "## Resposta" not in answer
            too_short = len(answer.strip()) < 120

            looks_bad = saw_hallucination or no_structure or too_short

            if looks_bad:
                reasons = []
                if too_short:        reasons.append(f"curta ({len(answer)} chars)")
                if no_structure:     reasons.append("sem '## Resposta final'")
                if saw_hallucination: reasons.append("indicador de alucinação")
                reason_str = ", ".join(reasons)
                log.warning("[vision] resposta ruim: %s — tentando fallback OCR", reason_str)

                if PROCESSING_MODE == "auto" and OCR_AVAILABLE \
                        and _is_installed(TEXT_MODEL, installed_models):
                    try:
                        result = await run_ocr_pipeline(
                            clean_image, user_message, installed_models, history
                        )
                        result["fallbackFrom"] = "vision-low-quality"
                        result["fallbackReason"] = (
                            f'A resposta do modelo de visão "{VISION_MODEL}" '
                            f"foi descartada ({reason_str}). "
                            f"Resolvi via OCR + {TEXT_MODEL}."
                        )
                        return result
                    except Exception as fb_err:
                        log.warning(
                            "[fallback] OCR também falhou: %s — devolvendo visão",
                            fb_err,
                        )
                        # Cai pra retornar a resposta original mesmo assim

            return {
                "ok": True,
                "provider": "ollama",
                "mode": "vision",
                "model": VISION_MODEL,
                "extractedText": None,
                "answer": answer,
            }

        except OllamaError as e:
            # FALLBACK AUTOMÁTICO: se a visão crashou (ex.: runner stopped),
            # tenta OCR sozinho. Só quando estamos em modo "auto" — em modo
            # "vision" explícito, propagamos o erro.
            should_fallback = (
                PROCESSING_MODE == "auto"
                and OCR_AVAILABLE
                and _is_installed(TEXT_MODEL, installed_models)
            )
            log.warning("[vision] falhou: %s | fallback=%s", e, should_fallback)

            if not should_fallback:
                # Repropaga com a mensagem detalhada
                detail = f'Ollama retornou erro ao usar o modelo "{e.model}":\n{e}'
                if e.hint:
                    detail += f"\n\n💡 {e.hint}"
                raise HTTPException(status_code=502, detail=detail)

            # Faz o fallback: roda o pipeline OCR.
            log.info("[fallback] caindo do modo vision para OCR")
            try:
                result = await run_ocr_pipeline(clean_image, user_message, installed_models, history)
                # Anota que houve fallback para o frontend mostrar
                result["fallbackFrom"] = "vision"
                result["fallbackReason"] = str(e)
                return result
            except HTTPException:
                raise
            except OllamaError as e2:
                detail = f'Visão falhou ({e}). OCR também falhou: {e2}'
                if e2.hint:
                    detail += f"\n\n💡 {e2.hint}"
                raise HTTPException(status_code=502, detail=detail)

    # ---------- 6) Modo OCR direto ----------
    try:
        return await run_ocr_pipeline(clean_image, user_message, installed_models, history)
    except OllamaError as e:
        detail = f'Ollama retornou erro ao usar o modelo "{e.model}":\n{e}'
        if e.hint:
            detail += f"\n\n💡 {e.hint}"
        raise HTTPException(status_code=502, detail=detail)


# ---------------------------------------------------------------------------
# Frontend estático (servido NA MESMA porta — basta abrir http://localhost:3001)
#
# IMPORTANTE: o mount fica DEPOIS das rotas da API (POST /chat, GET /health),
# pois rotas API são checadas primeiro. StaticFiles cuida do resto (GET /,
# GET /styles.css, GET /app.js, etc.).
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    log.warning("Pasta do frontend não encontrada em %s", FRONTEND_DIR)


# ---------------------------------------------------------------------------
# Permite rodar com:  python main.py
# (em produção use:   uvicorn main:app --host 0.0.0.0 --port 3001)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
