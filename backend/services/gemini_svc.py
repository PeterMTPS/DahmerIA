"""
services/gemini_svc.py
-----------------------
Cliente para a API do Google Gemini (cloud).

Por que Gemini?
  Modelos locais pequenos (moondream, llava-7b) NÃO conseguem ler com qualidade
  imagens com fórmulas, gráficos, manuscritos. Gemini 2.5 Flash lê com qualidade
  nível GPT-4 — sem precisar de OCR. É o que dá pra você estudar de verdade.

Como pegar uma chave (gratuita):
  1. Acesse https://aistudio.google.com/apikey
  2. "Create API key" — não precisa cartão de crédito
  3. Cole no backend/.env: GEMINI_API_KEY=sua-chave-aqui

Limites do free tier (mais que suficiente pra estudar):
  - gemini-2.5-flash: 15 requisições por minuto, 1.500 por dia
  - 1 milhão de tokens por minuto
"""

import io
import logging
import os

import google.generativeai as genai
from PIL import Image

log = logging.getLogger("dahmeria.gemini")


class GeminiError(Exception):
    """Erro vindo da API do Gemini, com hint pra resolver."""

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class GeminiService:
    def __init__(self):
        # Aceita aspas em volta da chave (vai que o usuário copiou com aspas)
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

        # available=True só se a chave existir E parecer válida
        # (Gemini keys começam com "AIza" e têm ~39 chars).
        self.available = bool(self.api_key) and len(self.api_key) > 20

        if self.available:
            try:
                genai.configure(api_key=self.api_key)
                log.info("Gemini configurado | modelo=%s | chave=%s***",
                         self.model_name, self.api_key[:6])
            except Exception as e:
                log.warning("Falha ao configurar Gemini: %s", e)
                self.available = False

    @property
    def info(self) -> dict:
        return {
            "available": self.available,
            "model": self.model_name if self.available else None,
            "has_key": bool(self.api_key),
        }

    @staticmethod
    def _convert_history(history: list[dict]) -> list[dict]:
        """Converte nosso formato {role: 'user'|'assistant', text: '...'}
        para o formato do Gemini SDK.

        IMPORTANTE: usar parts=[{"text": "..."}] (formato explícito) em vez de
        parts=["..."] (string solta). Algumas versões do SDK do google-generativeai
        silenciosamente IGNORAM o histórico no formato string e a IA "esquece" tudo.
        O dict explícito sempre funciona.
        """
        out = []
        for h in history or []:
            role = "user" if h.get("role") == "user" else "model"
            text = h.get("text", "")
            if text:
                out.append({
                    "role": role,
                    "parts": [{"text": text}],
                })
        return out

    async def chat_with_image(self, prompt: str, image_bytes: bytes,
                              history: list[dict]) -> str:
        """Gera resposta multimodal MANTENDO o contexto da conversa anterior.

        Usa a chat session do Gemini, que automaticamente preserva o histórico
        de turnos passados. A imagem é enviada apenas no turno atual.
        """
        if not self.available:
            raise GeminiError(
                "Gemini não está configurado.",
                hint=("Adicione GEMINI_API_KEY no backend/.env. "
                      "Pegue gratuita em https://aistudio.google.com/apikey."),
            )
        converted = self._convert_history(history)
        log.info("[gemini.chat_with_image] enviando %d turnos de histórico + imagem + prompt %d chars",
                 len(converted), len(prompt))
        try:
            model = genai.GenerativeModel(self.model_name)
            chat = model.start_chat(history=converted)
            img = Image.open(io.BytesIO(image_bytes))
            response = await chat.send_message_async([prompt, img])
            return (response.text or "").strip()
        except Exception as e:
            msg = str(e)
            log.warning("Gemini falhou (chat_with_image): %s", msg)
            raise GeminiError(msg, hint=self._build_hint(msg))

    async def chat_text(self, prompt: str, history: list[dict]) -> str:
        """Pergunta de texto puro mantendo contexto da conversa anterior."""
        if not self.available:
            raise GeminiError(
                "Gemini não configurado.",
                hint="Configure GEMINI_API_KEY no backend/.env.",
            )
        converted = self._convert_history(history)
        log.info("[gemini.chat_text] enviando %d turnos de histórico + prompt %d chars",
                 len(converted), len(prompt))
        try:
            model = genai.GenerativeModel(self.model_name)
            chat = model.start_chat(history=converted)
            response = await chat.send_message_async(prompt)
            return (response.text or "").strip()
        except Exception as e:
            msg = str(e)
            log.warning("Gemini falhou (chat_text): %s", msg)
            raise GeminiError(msg, hint=self._build_hint(msg))

    async def generate_with_image(self, prompt: str, image_bytes: bytes) -> str:
        """Manda prompt + imagem pro Gemini e devolve o texto da resposta.

        Gemini lê a imagem NATIVAMENTE — não converte para texto antes.
        Por isso ele consegue interpretar fórmulas, gráficos, scatter plots,
        manuscritos, diagramas geométricos.
        """
        if not self.available:
            raise GeminiError(
                "Gemini não está configurado.",
                hint=(
                    "Adicione GEMINI_API_KEY no backend/.env. "
                    "Pegue uma chave gratuita em https://aistudio.google.com/apikey "
                    "(não precisa cartão de crédito)."
                ),
            )

        try:
            model = genai.GenerativeModel(self.model_name)
            img = Image.open(io.BytesIO(image_bytes))
            # generate_content_async: SDK assíncrono nativo. Gemini aceita
            # uma lista [prompt, image] como entrada multimodal.
            response = await model.generate_content_async([prompt, img])
            return (response.text or "").strip()

        except Exception as e:
            msg = str(e)
            hint = self._build_hint(msg)
            log.warning("Gemini falhou: %s", msg)
            raise GeminiError(msg, hint=hint)

    async def generate_text(self, prompt: str) -> str:
        """Geração só com texto (sem imagem). Usado raramente."""
        if not self.available:
            raise GeminiError(
                "Gemini não configurado.",
                hint="Configure GEMINI_API_KEY no backend/.env.",
            )
        try:
            model = genai.GenerativeModel(self.model_name)
            response = await model.generate_content_async(prompt)
            return (response.text or "").strip()
        except Exception as e:
            raise GeminiError(str(e), hint=self._build_hint(str(e)))

    def _build_hint(self, msg: str) -> str | None:
        m = msg.lower()
        if "api_key_invalid" in m or "api key not valid" in m or "permission" in m:
            return ("Chave inválida ou sem permissão. Verifique GEMINI_API_KEY "
                    "no backend/.env. Pegue uma nova em https://aistudio.google.com/apikey")
        if "quota" in m or "429" in m or "resource_exhausted" in m:
            return ("Você atingiu o limite gratuito (15 req/min ou 1500 req/dia). "
                    "Espere um pouco antes de tentar de novo.")
        if "404" in m or "not found" in m:
            return (f'Modelo "{self.model_name}" não existe. Tente '
                    "GEMINI_MODEL=gemini-2.5-flash ou gemini-2.0-flash no .env.")
        if "blocked" in m or "safety" in m:
            return ("A resposta foi bloqueada pelos filtros de segurança do Gemini. "
                    "Tente reformular ou usar uma imagem diferente.")
        return None
