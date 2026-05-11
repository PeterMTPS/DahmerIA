/* =========================================================
   DahmerIA — frontend (vanilla JS)
   - Mantém estado simples (lista de mensagens em memória)
   - Faz upload da imagem (obrigatória) + dúvida opcional para POST /chat
   - Renderiza Markdown + LaTeX (KaTeX) + highlight de código
   ========================================================= */

// ---------- Helpers ----------

/** Faz escape básico de HTML (usado em conteúdo do usuário, nunca em Markdown da IA). */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

/**
 * Reduz a imagem para um tamanho seguro antes do upload.
 *
 * Por que? Modelos multimodais locais (LLaVA, etc) crasham facilmente com
 * fotos grandes — o "model runner has unexpectedly stopped" geralmente é
 * memória insuficiente. Forçar maxSide=1024 + JPEG 85% pega ~95% dos casos
 * sem perder legibilidade do enunciado.
 *
 * Retorna um File novo (com mesmo nome) ou o original se já for pequeno.
 */
async function resizeImageIfNeeded(file, maxSide = 1024, quality = 0.85) {
  // Se já é pequeno, não toca.
  if (file.size < 400 * 1024) return file;

  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });

  const img = await new Promise((resolve, reject) => {
    const i = new Image();
    i.onload = () => resolve(i);
    i.onerror = reject;
    i.src = dataUrl;
  });

  // Calcula novas dimensões mantendo proporção
  let { width, height } = img;
  const longest = Math.max(width, height);
  if (longest > maxSide) {
    const scale = maxSide / longest;
    width = Math.round(width * scale);
    height = Math.round(height * scale);
  }

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, width, height);

  const blob = await new Promise(resolve =>
    canvas.toBlob(resolve, 'image/jpeg', quality)
  );

  // Se por algum motivo o resize falhou, devolve o original
  if (!blob) return file;

  // Renomeia mantendo extensão lógica para o backend (vai como image/jpeg)
  const newName = file.name.replace(/\.[^.]+$/, '') + '.jpg';
  return new File([blob], newName, { type: 'image/jpeg' });
}

/** Rola o chat para o fim de forma suave. */
function scrollToBottom() {
  // Usamos setTimeout para esperar o DOM atualizar antes de medir altura.
  setTimeout(() => {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  }, 50);
}

/** Renderiza Markdown + LaTeX no elemento alvo. */
function renderRichContent(el, markdownText) {
  // 1) Markdown -> HTML
  el.innerHTML = marked.parse(markdownText, {
    breaks: true,        // \n vira <br>
    gfm: true,           // GitHub-flavored Markdown (tabelas, etc)
  });

  // 2) "Desempacota" formulas LaTeX que a IA envolveu em `code` (backticks).
  //    Modelos como Gemini às vezes escrevem `$x^2$` em vez de $x^2$, o que
  //    vira <code>$x^2$</code> e o KaTeX ignora (ele pula tags <code>).
  //    Aqui detectamos esse padrão e desfazemos antes do KaTeX rodar.
  el.querySelectorAll('code').forEach(codeEl => {
    // Não toca em blocos de código de verdade (dentro de <pre>).
    if (codeEl.parentElement && codeEl.parentElement.tagName === 'PRE') return;
    const text = codeEl.textContent || '';
    const looksLikeLatex =
      /^\s*\$\$[\s\S]+\$\$\s*$/.test(text) ||      // $$ ... $$
      /^\s*\$[^$\n]+\$\s*$/.test(text)    ||      // $ ... $
      /^\s*\\\(.+\\\)\s*$/.test(text)     ||      // \( ... \)
      /^\s*\\\[[\s\S]+\\\]\s*$/.test(text);       // \[ ... \]
    if (looksLikeLatex) {
      codeEl.replaceWith(document.createTextNode(text));
    }
  });

  // 3) Highlight de blocos de código de verdade (linguagens de programação)
  el.querySelectorAll('pre code').forEach(block => {
    try { hljs.highlightElement(block); } catch (_) { /* ignora */ }
  });

  // 4) Renderiza LaTeX ($...$ e $$...$$)
  if (window.renderMathInElement) {
    renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true  },
        { left: '$',  right: '$',  display: false },
        { left: '\\(', right: '\\)', display: false },
        { left: '\\[', right: '\\]', display: true  },
      ],
      throwOnError: false,
      // Não pula <code> — em alguns casos a IA escreve fórmula em <code>
      // mesmo. Já tratamos isso acima, mas ignoredTags vazio é seguro.
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre'],
    });
  }
}

// ---------- Estado ----------

const state = {
  /** mensagens já trocadas (para histórico visual; o backend é stateless por enquanto) */
  messages: [],
  /** arquivo selecionado no input (File | null) */
  imageFile: null,
  /** flag para impedir múltiplos envios simultâneos */
  sending: false,
};

// ---------- Elementos do DOM ----------

const chatEl       = document.getElementById('chat');
const formEl       = document.getElementById('form');
const imageInput   = document.getElementById('image');
const messageInput = document.getElementById('message');
const sendBtn      = document.getElementById('send');
const previewEl    = document.getElementById('preview');
const previewImg   = document.getElementById('preview-img');
const previewRm    = document.getElementById('preview-remove');
const uploadLabel  = document.querySelector('.upload-btn');
const hintEl       = document.getElementById('hint');
const statusDot    = document.getElementById('status-dot');
const statusText   = document.getElementById('status-text');
const providerSel  = document.getElementById('provider');
const resetBtn     = document.getElementById('reset-btn');

const PROVIDER_LS_KEY = 'dahmeria.provider';
const HISTORY_LS_KEY  = 'dahmeria.history';

// ---------- Renderização das mensagens ----------

/**
 * Adiciona uma mensagem do USUÁRIO no chat.
 * @param {string} text - dúvida opcional escrita pelo usuário
 * @param {string} imageUrl - URL temporária (object URL) da imagem
 */
function appendUserMessage(text, imageUrl) {
  const wrap = document.createElement('div');
  wrap.className = 'message message-user';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  if (text) {
    const p = document.createElement('p');
    p.innerHTML = escapeHtml(text);
    bubble.appendChild(p);
  }

  if (imageUrl) {
    const img = document.createElement('img');
    img.src = imageUrl;
    img.alt = 'Imagem da questão enviada';
    bubble.appendChild(img);
  }

  wrap.appendChild(bubble);
  chatEl.appendChild(wrap);
  scrollToBottom();
}

/**
 * Adiciona uma mensagem da IA (com loading inicial).
 * Retorna um objeto com método `update(answer, meta)` para preencher depois.
 */
function appendBotMessage() {
  const wrap = document.createElement('div');
  wrap.className = 'message message-bot';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  // Loading inicial (3 bolinhas)
  bubble.innerHTML = `
    <div class="typing" aria-label="A IA está pensando">
      <span></span><span></span><span></span>
    </div>
  `;

  wrap.appendChild(bubble);
  chatEl.appendChild(wrap);
  scrollToBottom();

  return {
    /** Substitui o loading pelo conteúdo final. */
    update(answer, meta = {}) {
      bubble.innerHTML = '';

      // Caixa do OCR (só aparece se backend retornou texto extraído)
      if (meta.extractedText) {
        const ocr = document.createElement('div');
        ocr.className = 'ocr-box';
        ocr.textContent = meta.extractedText;
        bubble.appendChild(ocr);
      }

      // Conteúdo principal (Markdown -> HTML + LaTeX)
      const content = document.createElement('div');
      content.className = 'bot-content';
      bubble.appendChild(content);
      renderRichContent(content, answer);

      // Toolbar com botão "Copiar"
      const toolbar = document.createElement('div');
      toolbar.className = 'bot-toolbar';

      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'copy-btn';
      copyBtn.textContent = '📋 Copiar';
      copyBtn.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(answer);
          copyBtn.textContent = '✓ Copiado';
          copyBtn.classList.add('copied');
          setTimeout(() => {
            copyBtn.textContent = '📋 Copiar';
            copyBtn.classList.remove('copied');
          }, 1800);
        } catch (_e) {
          copyBtn.textContent = 'Falhou';
        }
      });
      toolbar.appendChild(copyBtn);
      bubble.appendChild(toolbar);

      scrollToBottom();
    },
    /** Mostra erro no lugar do loading. */
    fail(errorMessage) {
      // white-space: pre-wrap preserva quebras de linha que o backend manda
      // (ex.: dica "💡 Rode: ollama pull llava" numa linha separada).
      bubble.innerHTML = `
        <p><strong>⚠️ Erro</strong></p>
        <p style="white-space: pre-wrap; margin-top: 6px;">${escapeHtml(errorMessage)}</p>
      `;
      scrollToBottom();
    },
  };
}

// ---------- Eventos do formulário ----------

/** Atualiza o estado do botão de envio: libera se houver TEXTO ou IMAGEM. */
function refreshSendState() {
  const hasText = messageInput.value.trim().length > 0;
  const hasImage = !!state.imageFile;
  const ready = (hasText || hasImage) && !state.sending;
  sendBtn.disabled = !ready;

  hintEl.classList.remove('error');
  if (state.sending) {
    hintEl.textContent = 'Processando... isso pode levar alguns segundos.';
  } else if (!hasText && !hasImage) {
    hintEl.textContent = 'Escreva uma pergunta ou anexe uma imagem.';
  } else if (hasImage && hasText) {
    hintEl.textContent = 'Imagem + texto. Aperte enviar (Enter).';
  } else if (hasImage) {
    hintEl.textContent = 'Imagem anexada. Aperte enviar (Enter).';
  } else {
    hintEl.textContent = 'Aperte enviar (Enter).';
  }
}

/** Trata seleção de imagem. */
imageInput.addEventListener('change', () => {
  const file = imageInput.files && imageInput.files[0];
  if (!file) {
    clearImage();
    return;
  }

  if (!file.type.startsWith('image/')) {
    showError('O arquivo selecionado não é uma imagem.');
    clearImage();
    return;
  }

  state.imageFile = file;

  // Cria URL temporária para preview
  const url = URL.createObjectURL(file);
  previewImg.src = url;
  previewEl.classList.remove('hidden');
  uploadLabel.classList.add('has-image');

  refreshSendState();
});

/** Botão "x" do preview: remove imagem selecionada. */
previewRm.addEventListener('click', clearImage);

function clearImage() {
  state.imageFile = null;
  imageInput.value = '';
  previewImg.src = '';
  previewEl.classList.add('hidden');
  uploadLabel.classList.remove('has-image');
  refreshSendState();
}

/** Auto-resize do textarea + atualiza estado do botão conforme digita. */
messageInput.addEventListener('input', () => {
  messageInput.style.height = 'auto';
  messageInput.style.height = Math.min(messageInput.scrollHeight, 160) + 'px';
  refreshSendState();
});

/** Enter envia, Shift+Enter quebra linha. */
messageInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

/** Submissão do formulário: envia para o backend. */
formEl.addEventListener('submit', async (e) => {
  e.preventDefault();
  const userText = messageInput.value.trim();
  const hasImage = !!state.imageFile;

  // Precisa pelo menos um dos dois
  if ((!userText && !hasImage) || state.sending) return;

  const imageObjectUrl = hasImage ? URL.createObjectURL(state.imageFile) : null;

  // 1) Mostra a mensagem do usuário no chat
  appendUserMessage(userText, imageObjectUrl);

  // 2) Cria bolha da IA com loading
  const botMessage = appendBotMessage();

  // 3) Reseta UI imediatamente (UX)
  state.sending = true;
  refreshSendState();
  messageInput.value = '';
  messageInput.style.height = 'auto';
  const originalFile = state.imageFile;
  clearImage();

  try {
    // 4) Prepara payload multipart
    const fd = new FormData();

    // Imagem só se o usuário anexou
    if (originalFile) {
      const fileToSend = await resizeImageIfNeeded(originalFile);
      fd.append('image', fileToSend);
    }
    if (userText) fd.append('message', userText);
    const chosenProvider = providerSel.value || '';
    if (chosenProvider) fd.append('provider', chosenProvider);

    // 5b) Histórico da conversa atual (memória pra IA)
    // Manda só os turnos com texto — o backend ignora o resto.
    const history = state.messages
      .filter(m => m && m.text)
      .map(m => ({ role: m.role, text: m.text }));
    fd.append('history', JSON.stringify(history));

    const res = await fetch('/chat', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));

    if (!res.ok || !data.ok) {
      throw new Error(data.error || `Falha (HTTP ${res.status}).`);
    }

    botMessage.update(data.answer || '*(resposta vazia)*', {
      extractedText: data.extractedText,
      mode: data.mode,
      model: data.model,
    });

    state.messages.push(
      { role: 'user', text: userText, file: originalFile?.name || null },
      { role: 'assistant', text: data.answer, mode: data.mode, model: data.model }
    );
    saveHistory();
  } catch (err) {
    botMessage.fail(err.message || 'Erro inesperado.');
  } finally {
    state.sending = false;
    refreshSendState();
  }
});

function showError(msg) {
  hintEl.textContent = msg;
  hintEl.classList.add('error');
}

// ---------- Provider selector + status do header ----------

/**
 * Popula o <select> de providers baseado no que o backend reporta.
 * Mantém a seleção do usuário (localStorage) se ainda for válida.
 */
function populateProviderSelector(health) {
  const providers = health.providers || {};
  const options = [];

  if (providers.gemini && providers.gemini.available) {
    options.push({
      value: 'gemini',
      label: `🌐 Gemini (${providers.gemini.model || 'cloud'})`,
    });
  } else {
    options.push({
      value: 'gemini',
      label: '🌐 Gemini (configurar API key)',
      disabled: true,
    });
  }

  if (providers.ollama && providers.ollama.available) {
    const ol = providers.ollama;
    const modelLabel = ol.mode === 'ocr'
      ? `${ol.textModel} (OCR)`
      : `${ol.visionModel} (visão)`;
    options.push({
      value: 'ollama',
      label: `🖥️ Ollama (${modelLabel})`,
    });
  } else {
    options.push({
      value: 'ollama',
      label: '🖥️ Ollama (offline)',
      disabled: true,
    });
  }

  // Recupera última escolha do usuário ou usa default do servidor
  const saved = localStorage.getItem(PROVIDER_LS_KEY);
  const desired = saved || health.defaultProvider || 'gemini';

  // Reconstrói o <select>
  providerSel.innerHTML = '';
  for (const opt of options) {
    const el = document.createElement('option');
    el.value = opt.value;
    el.textContent = opt.label;
    if (opt.disabled) el.disabled = true;
    providerSel.appendChild(el);
  }

  // Tenta selecionar o desejado; se desabilitado, escolhe o primeiro habilitado
  const desiredOpt = providerSel.querySelector(`option[value="${desired}"]`);
  if (desiredOpt && !desiredOpt.disabled) {
    providerSel.value = desired;
  } else {
    const firstEnabled = providerSel.querySelector('option:not([disabled])');
    if (firstEnabled) providerSel.value = firstEnabled.value;
  }

  providerSel.disabled = false;
}

providerSel.addEventListener('change', () => {
  localStorage.setItem(PROVIDER_LS_KEY, providerSel.value);
});

async function checkHealth() {
  try {
    const res = await fetch('/health');
    const data = await res.json();
    populateProviderSelector(data);

    const gem = data.providers?.gemini;
    const ol  = data.providers?.ollama;
    const anyOnline = (gem && gem.available) || (ol && ol.available);

    if (anyOnline) {
      statusDot.className = 'dot online';
      const parts = [];
      if (gem && gem.available) parts.push('Gemini');
      if (ol  && ol.available)  parts.push('Ollama');
      statusText.textContent = `Online: ${parts.join(' + ')}`;
    } else {
      statusDot.className = 'dot offline';
      statusText.textContent = 'Nenhum provider disponível';
    }
  } catch {
    statusDot.className = 'dot offline';
    statusText.textContent = 'Backend indisponível';
  }
}

// ---------- Histórico persistente (localStorage) ----------

/** Salva o histórico atual no localStorage pra sobreviver a refresh. */
function saveHistory() {
  try {
    const slim = state.messages.map(m => ({
      role: m.role,
      text: m.text,
      // Não salva blob de imagem — pesado e não dá pra reidratar
    }));
    localStorage.setItem(HISTORY_LS_KEY, JSON.stringify(slim));
  } catch (_e) { /* quota cheia, ignora */ }
}

/** Restaura o histórico salvo e replica as bolhas no chat. */
function loadAndReplayHistory() {
  let raw;
  try { raw = localStorage.getItem(HISTORY_LS_KEY); } catch { return; }
  if (!raw) return;

  let data;
  try { data = JSON.parse(raw); } catch { return; }
  if (!Array.isArray(data) || data.length === 0) return;

  state.messages = data;

  // Replica as bolhas na UI (sem imagens — só texto)
  for (const m of data) {
    if (!m || !m.text) continue;
    if (m.role === 'user') {
      appendUserMessage(m.text, null);
    } else if (m.role === 'assistant') {
      const bot = appendBotMessage();
      bot.update(m.text, {});
    }
  }
}

/** Limpa toda a conversa (UI + memória + storage). */
function resetConversation() {
  if (state.messages.length === 0) {
    // Só pisca o botão pra dar feedback visual
    resetBtn.style.transform = 'rotate(-360deg)';
    setTimeout(() => { resetBtn.style.transform = ''; }, 400);
    return;
  }
  if (!confirm('Limpar a conversa atual e começar do zero?')) return;
  state.messages = [];
  try { localStorage.removeItem(HISTORY_LS_KEY); } catch {}
  // Remove todas as mensagens menos a de boas-vindas
  chatEl.querySelectorAll('.message:not(.welcome)').forEach(el => el.remove());
}

resetBtn.addEventListener('click', resetConversation);

// ---------- Inicialização ----------

// Checa status na carga inicial e a cada 20s
checkHealth();
setInterval(checkHealth, 20000);

// Restaura conversa anterior se houver
loadAndReplayHistory();

// Estado inicial do botão
refreshSendState();
