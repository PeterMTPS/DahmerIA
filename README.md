# DahmerIA — Assistente de Estudos local (Ollama + Python)

Aplicação web completa que ajuda estudantes a resolver questões a partir de **uma foto da prova**.
Você envia a imagem, a IA lê o enunciado, identifica a disciplina e explica **passo a passo** em português.

Tudo roda **localmente** via [Ollama](https://ollama.com) — nada vai pra nuvem.

---

## ✨ Funcionalidades

- 📸 **Upload de imagem obrigatório** (JPG, PNG, WEBP — até 10 MB)
- 🧠 **Dois modos de leitura**, com escolha automática:
  - **Visão (multimodal)**: usa LLaVA / moondream / llama3.2-vision e "vê" a imagem direto
  - **OCR (Tesseract)**: extrai o texto da imagem e manda pra um modelo de texto
- 🛟 **Fallback automático**: se a visão crashar, cai pro OCR sem você mexer no `.env`
- 🧹 **Pré-processamento de imagem com Pillow**: converte pra RGB, remove EXIF, redimensiona pra 672px — evita os crashes "model runner has unexpectedly stopped" do Ollama
- 💬 Interface estilo chat com histórico
- 🧮 Renderização de **fórmulas matemáticas** com KaTeX (LaTeX)
- 📝 Suporte a **Markdown** e syntax highlight
- 📋 Botão de **copiar resposta**
- 🟢 Indicador de status do Ollama no header

---

## 📁 Estrutura do projeto

```
DahmerIA/
├── backend/                       # Python + FastAPI
│   ├── main.py                    # App principal: rotas /chat e /health
│   ├── requirements.txt
│   ├── .env.example               # Copie para .env
│   └── services/
│       ├── image_proc.py          # Pillow: normaliza imagem (RGB, sem EXIF, ≤672px)
│       ├── ocr.py                 # pytesseract (precisa do binário Tesseract)
│       └── ollama_svc.py          # Cliente HTTP do Ollama (httpx + streaming NDJSON)
├── frontend/
│   ├── index.html                 # Marcação da interface de chat
│   ├── styles.css                 # Tema escuro, responsivo
│   └── app.js                     # Lógica do chat (vanilla JS)
└── README.md
```

---

## 🛠️ Pré-requisitos

1. **Python 3.10+** — [python.org/downloads](https://www.python.org/downloads/)
2. **Ollama** instalado e rodando — [ollama.com/download](https://ollama.com/download)
3. **Tesseract OCR** (opcional, só se quiser modo OCR):
   - Windows: [github.com/UB-Mannheim/tesseract/wiki](https://github.com/UB-Mannheim/tesseract/wiki)
     (durante a instalação, marque os idiomas Portuguese e English)
   - Linux: `sudo apt install tesseract-ocr tesseract-ocr-por`
   - macOS: `brew install tesseract tesseract-lang`
4. Pelo menos **um modelo** baixado no Ollama:
   ```bash
   # Para o modo VISÃO (recomendado se tiver GPU 6GB+):
   ollama pull llava

   # Alternativa LEVE pra GPUs/RAM modestas:
   ollama pull moondream

   # Para o modo OCR (fallback ou principal):
   ollama pull llama3
   ```

---

## 🚀 Como rodar (passo a passo)

### 1) Instale o Ollama e baixe um modelo

```bash
ollama pull llama3
ollama pull moondream    # opcional, pra modo visão
```

### 2) Crie um ambiente virtual Python

**Windows (PowerShell):**
```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**Windows (CMD):**
```cmd
cd backend
python -m venv .venv
.venv\Scripts\activate.bat
```

**Linux / macOS:**
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
```

### 3) Instale as dependências

```bash
pip install -r requirements.txt
```

### 4) Configure o `.env`

```bash
cp .env.example .env
```

(No Windows: `copy .env.example .env`)

Edite se quiser. Os defaults funcionam:

```
PORT=3001
OLLAMA_URL=http://localhost:11434
OLLAMA_TEXT_MODEL=llama3
OLLAMA_VISION_MODEL=llava
PROCESSING_MODE=auto       # auto | vision | ocr
OCR_LANGS=por+eng
TESSERACT_CMD=             # só preencha se Tesseract não está no PATH
```

**No Windows**, se você instalou o Tesseract no caminho padrão e não o adicionou ao PATH, descomente e ajuste:
```
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

### 5) Rode o servidor

```bash
python main.py
```

Ou, com auto-reload pra desenvolvimento:
```bash
uvicorn main:app --reload --port 3001
```

Você verá:
```
==================================================
 DahmerIA - Assistente de Estudos (local)
==================================================
 Frontend / Backend: http://localhost:3001
 Ollama esperado em: http://localhost:11434
 Modo:               auto
 Modelo texto:       llama3
 Modelo visão:       llava
 OCR (Tesseract):    OK
==================================================
```

### 6) Abra o navegador

Acesse [http://localhost:3001](http://localhost:3001), anexe a foto de uma questão e mande ver. ✅

---

## 🔌 Endpoints da API

### `POST /chat`

Recebe uma imagem (obrigatória) + texto opcional. Retorna a resolução em Markdown.

**Campos** (`multipart/form-data`):

| Campo     | Tipo   | Obrigatório | Descrição                                   |
|-----------|--------|-------------|---------------------------------------------|
| `image`   | File   | ✅ sim       | Foto da questão (JPG/PNG/WEBP, máx. 10 MB)  |
| `message` | String | ❌ não       | Dúvida ou observação extra do estudante     |

**Exemplo com `curl`:**

```bash
curl -X POST http://localhost:3001/chat \
  -F "image=@./questao.png" \
  -F "message=Não entendi por que dá positivo no final"
```

**Resposta (200):**

```json
{
  "ok": true,
  "mode": "vision",
  "model": "llava",
  "extractedText": null,
  "answer": "## Tema: Matemática — Equação de 2º grau\n\n**Enunciado:** ..."
}
```

Se houver fallback automático visão→OCR, a resposta inclui `fallbackFrom: "vision"` e `fallbackReason`.

**Erros comuns:**

| Status | Significado                                                              |
|--------|--------------------------------------------------------------------------|
| 400    | Imagem ausente / inválida                                                |
| 413    | Imagem maior que 10 MB                                                   |
| 422    | OCR não conseguiu extrair texto da imagem                                |
| 502    | Ollama retornou erro (modelo crashou, contexto excedido, etc.)           |
| 503    | Ollama offline OU modelo necessário não instalado                        |

### `GET /health`

Retorna status do Ollama, modelos disponíveis e se Tesseract está instalado:

```bash
curl http://localhost:3001/health
```

```json
{
  "ok": true,
  "ollama": "online",
  "models": ["llava:latest", "llama3:latest"],
  "ocr_available": true,
  "config": {
    "mode": "auto",
    "textModel": "llama3",
    "visionModel": "llava"
  }
}
```

---

## 🎯 Dicas de uso

- **Foto nítida = resposta melhor.** Boa iluminação, sem dedos cobrindo o enunciado.
- **No modo OCR**, a IA depende 100% do que o Tesseract leu. O frontend mostra o texto extraído pra você conferir.
- **No modo visão**, a IA lê a imagem original — mais robusto a manuscritos e diagramas. Mas exige mais RAM/VRAM.
- **Hardware modesto?** `moondream` (1.8B) cabe em 2GB de VRAM e é rápido.
- Para questões em inglês, edite `OCR_LANGS=eng` no `.env`.

---

## 🧩 Solução de problemas

**"Ollama offline" no header**
→ Rode `ollama serve` ou abra o app do Ollama. Confira `OLLAMA_URL` no `.env`.

**"Tesseract OCR não está instalado"**
→ Baixe e instale o Tesseract (link nos pré-requisitos). Se já instalou e não funciona, defina `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe` no `.env`.

**"model runner has unexpectedly stopped"**
→ O modelo de visão crashou. A app já tenta fallback pro OCR automaticamente em modo `auto`. Se quiser evitar de vez, edite `.env` com `PROCESSING_MODE=ocr`.

**"Não consegui ler nenhum texto na imagem"**
→ A foto está borrada/distante. Tire outra mais perto e bem iluminada. OU instale `llava`/`moondream` para o modo visão.

**Resposta demora muito**
→ Modelos locais são lentos no CPU. Use um modelo menor (ex.: `phi3`, `moondream`) ou GPU.

**Erro `model not found`**
→ Você definiu um modelo no `.env` que não foi baixado. Rode `ollama pull <modelo>` ou ajuste o `.env`.

**`pytesseract.TesseractNotFoundError`**
→ O binário do Tesseract não está no PATH. Adicione `C:\Program Files\Tesseract-OCR` ao PATH do Windows OU configure `TESSERACT_CMD` no `.env`.

---

## 📜 Licença

Use livremente para estudo. ✌️
