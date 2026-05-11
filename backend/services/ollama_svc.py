"""
services/ollama_svc.py
-----------------------
Cliente assíncrono pra API HTTP do Ollama (http://localhost:11434).

Usa STREAMING (NDJSON) sempre. Por que?
  Modelos multimodais no Windows com `stream: false` batem em um bug
  conhecido do runner do Ollama que mata o processo do modelo. Streaming
  evita isso entregando token a token. Acumulamos tudo aqui e devolvemos
  a resposta completa pra rota — o frontend não percebe diferença.
"""

import base64
import json
from typing import List, Optional

import httpx


class OllamaError(Exception):
    """Erro originado pelo Ollama (modelo não encontrado, runner crashou, etc.).

    Carrega a mensagem REAL extraída do corpo da resposta — não a genérica
    "Request failed with status 500" que vem do cliente HTTP.
    """

    def __init__(self, message: str, *, status: Optional[int] = None,
                 model: Optional[str] = None, hint: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.model = model
        self.hint = hint


def _build_hint(message: str, status: Optional[int], model: str) -> Optional[str]:
    """Gera dica acionável baseada no tipo de erro do Ollama."""
    msg_lower = message.lower()
    if status == 404 or "not found" in msg_lower:
        return f'Rode "ollama pull {model}" para baixar o modelo.'
    if "runner" in msg_lower and "stopped" in msg_lower:
        return (
            "O runner do modelo crashou. Causas comuns:\n"
            "   1) Falta de memória (RAM/VRAM) — feche outros apps\n"
            "   2) Driver de GPU desatualizado — atualize o NVIDIA/AMD\n"
            "   3) Versão antiga do Ollama — atualize em ollama.com/download\n"
            "   Se persistir, mude PROCESSING_MODE=ocr no .env (modo leve)."
        )
    if "out of memory" in msg_lower or "cuda" in msg_lower:
        return "Falta de VRAM. Use um modelo menor (ex.: moondream) ou PROCESSING_MODE=ocr."
    if "context" in msg_lower and ("length" in msg_lower or "too long" in msg_lower):
        return "Prompt maior que o contexto do modelo. Recorte o enunciado."
    return None


class OllamaService:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Listagem de modelos
    # ------------------------------------------------------------------
    async def list_models(self) -> List[str]:
        """Retorna nomes dos modelos instalados (ex.: ['llava:latest']).
        Devolve lista vazia se o Ollama estiver offline."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                data = r.json()
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Geração de texto
    # ------------------------------------------------------------------
    async def generate_text(self, model: str, prompt: str, *,
                            temperature: float = 0.1,
                            timeout: float = 120.0) -> str:
        """Gera resposta a partir de texto puro (sem imagem)."""
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "options": {"temperature": temperature},
        }
        return await self._stream_generate(payload, timeout=timeout)

    # ------------------------------------------------------------------
    # Geração multimodal (visão)
    # ------------------------------------------------------------------
    async def generate_with_image(self, model: str, prompt: str,
                                  image_bytes: bytes, *,
                                  timeout: float = 180.0,
                                  num_predict: int = 600) -> str:
        """Gera resposta a partir de prompt + imagem (LLaVA, moondream, etc.).

        image_bytes deve ser uma imagem JÁ pré-processada (RGB, sem EXIF,
        ≤ 672px). Veja services/image_proc.preprocess_image().
        """
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [b64],
            "stream": True,
            # num_predict limita a saída para não estourar memória.
            # Sem num_ctx forçado: deixar o default do modelo é mais estável.
            "options": {"num_predict": num_predict},
        }
        return await self._stream_generate(payload, timeout=timeout)

    # ------------------------------------------------------------------
    # Núcleo: faz POST em /api/generate em streaming e acumula a resposta
    # ------------------------------------------------------------------
    async def _stream_generate(self, payload: dict, timeout: float) -> str:
        url = f"{self.base_url}/api/generate"
        model_name = payload.get("model", "?")
        full = []

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=payload) as r:

                    # Erro HTTP (404 modelo não existe, 500, etc.):
                    # leemos o corpo INTEIRO e tentamos extrair { error: "..." }.
                    if r.status_code != 200:
                        raw = (await r.aread()).decode("utf-8", errors="replace")
                        message = raw
                        try:
                            parsed = json.loads(raw)
                            if isinstance(parsed, dict) and "error" in parsed:
                                message = parsed["error"]
                        except Exception:
                            pass
                        raise OllamaError(
                            message,
                            status=r.status_code,
                            model=model_name,
                            hint=_build_hint(message, r.status_code, model_name),
                        )

                    # Sucesso: stream NDJSON. Cada linha é um JSON com {response, done, ...}.
                    buffer = ""
                    async for chunk in r.aiter_text():
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                # linha pode estar partida; ignora e espera mais
                                continue

                            # Erro reportado durante a geração (runner crashou no meio):
                            if "error" in obj:
                                err_msg = obj["error"]
                                raise OllamaError(
                                    err_msg,
                                    status=500,
                                    model=model_name,
                                    hint=_build_hint(err_msg, 500, model_name),
                                )

                            # Pedaço normal de resposta:
                            piece = obj.get("response")
                            if isinstance(piece, str):
                                full.append(piece)

        except OllamaError:
            raise
        except httpx.ConnectError as e:
            raise OllamaError(
                f"Não consegui conectar ao Ollama em {self.base_url}.",
                status=None, model=model_name,
                hint='Rode "ollama serve" ou abra o app do Ollama.',
            ) from e
        except httpx.ReadTimeout as e:
            raise OllamaError(
                "Ollama demorou demais pra responder (timeout).",
                status=None, model=model_name,
                hint="Modelos grandes em CPU são lentos — use um modelo menor.",
            ) from e
        except Exception as e:
            raise OllamaError(
                f"Erro inesperado falando com o Ollama: {e}",
                status=None, model=model_name,
            ) from e

        return "".join(full)
