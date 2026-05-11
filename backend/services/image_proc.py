"""
services/image_proc.py
-----------------------
Pré-processamento de imagem ANTES de mandar para o Ollama.

Por que esse arquivo existe?
  Modelos multimodais (LLaVA, moondream) crasham com mensagem genérica
  "model runner has unexpectedly stopped" quando a imagem tem:
    - EXIF malformado
    - Espaço de cor estranho (CMYK, P, RGBA com alpha quebrado)
    - Dimensões muito grandes
    - Codificação JPEG fora do padrão (algumas câmeras Android)

  A CLI do Ollama (`ollama run llava arquivo.jpg`) NÃO crasha porque
  internamente ela passa a imagem por um pipeline parecido ao deste módulo.

  Este módulo replica esse comportamento: abre a imagem com Pillow, remove
  qualquer metadado, força RGB, redimensiona pra um tamanho seguro e salva
  como JPEG limpo. O resultado vai para o Ollama com bytes garantidamente
  bem formados.
"""

import io

from PIL import Image, ImageOps


# 672px é metade da resolução típica de fotos de celular pós-resize do
# frontend. Modelos como LLaVA/moondream foram treinados em 336-672px,
# então mandar mais que isso só gasta memória sem ganhar qualidade.
DEFAULT_MAX_SIDE = 672
DEFAULT_QUALITY = 88


def preprocess_image(
    raw_bytes: bytes,
    max_side: int = DEFAULT_MAX_SIDE,
    quality: int = DEFAULT_QUALITY,
) -> bytes:
    """
    Recebe bytes brutos de uma imagem (qualquer formato suportado pelo
    Pillow: JPEG, PNG, WEBP, BMP, etc.) e devolve bytes de um JPEG
    "limpo" — sem EXIF, em RGB, ≤ max_side de lado.

    Levanta ValueError se os bytes não forem uma imagem válida.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
    except Exception as e:
        raise ValueError(f"arquivo não é uma imagem válida ({e})")

    # 1) Auto-rotaciona baseado no EXIF (foto tirada deitada → fica em pé).
    #    O exif_transpose também REMOVE a tag de orientação, evitando
    #    rotação dupla em viewers que respeitam EXIF.
    img = ImageOps.exif_transpose(img)

    # 2) Garante RGB. Modelos de visão esperam 3 canais.
    #    - L (cinza) -> RGB triplica o canal
    #    - RGBA -> "achata" sobre fundo branco (evita problemas com transparência)
    #    - CMYK / P -> converte direto
    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # 3) Redimensiona mantendo proporção. thumbnail() só reduz, nunca aumenta.
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    # 4) Re-salva como JPEG novo. Isso descarta TODOS os metadados
    #    (EXIF, ICC profile, comentários XMP, etc.) — exatamente o que
    #    queremos pra evitar crashes do Ollama.
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
