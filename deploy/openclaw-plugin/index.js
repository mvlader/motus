// Мост openclaw → MOTUS. Формат — как у остальных плагинов openclaw
// (~/.openclaw/extensions/<id>/index.js, плоский объект с register(api)).
//
// Два хука:
//
// before_prompt_build — перед каждым ходом агента:
//   1. отдаёт текст сообщения пользователя в MOTUS (POST /event);
//   2. тянет свежую карточку (GET /state/card) и дописывает её в конец промпта;
//   3. при config.applyGate=true сужает набор инструментов хода до gate.allowed_tools.
//
// message_sending — прямо перед тем, как готовый текст уйдёт в канал:
//   при config.applyGate=true — жёстко (не просьбой в промпте, а самим хуком):
//     - если gate.forbidden содержит "outbound" (например, режим RAGE) — отменяет
//       отправку целиком (cancel: true). Директивы в карточке — просьба, которую
//       модель может не заметить; это тот единственный случай, где вместо
//       просьбы стоит жёсткий блок;
//     - иначе обрезает текст под gate.max_tokens (грубая оценка символов на
//       токен — токенайзер недоступен из плагина, см. CHARS_PER_TOKEN).
//   При applyGate=false — не трогает исходящее вовсе (совместимо со старым
//   поведением, только карточка в промпте).
//
// MOTUS не ответил за timeoutMs → ход идёт как обычно, ничего не блокируется и не
// обрезается (fail open — молчание MOTUS не должно ронять доставку). Плагин видит
// только публичный API: ни чисел состояния, ни журнала.

//: Грубая оценка символов на токен для обрезки message_sending. Настоящего
//: токенайзера у плагина нет; ru-текст в среднем плотнее по токенам, чем en,
//: поэтому оценка консервативная (меньше символов на токен, чем обычно для
//: английского ~4) — лучше обрезать чуть раньше настоящего лимита, чем позже.
const CHARS_PER_TOKEN = 2.6;

function truncateForBudget(text, maxTokens, charsPerToken) {
  const budget = Math.max(40, Math.floor(maxTokens * charsPerToken));
  if (text.length <= budget) return text;
  let cut = text.slice(0, budget);
  const lastSpace = cut.lastIndexOf(" ");
  if (lastSpace > budget * 0.6) cut = cut.slice(0, lastSpace);
  return cut.trimEnd() + "…";
}

export default {
  id: "motus",
  name: "MOTUS",
  register(api) {
    const cfg = api.pluginConfig || {};
    const base = (cfg.url || "http://127.0.0.1:18790").replace(/\/$/, "");
    const timeoutMs = cfg.timeoutMs || 2000;
    const applyGate = cfg.applyGate === true;
    const charsPerToken = Number(cfg.charsPerToken) || CHARS_PER_TOKEN;
    const log = api.logger || console;

    const call = (path, init) =>
      fetch(base + path, { signal: AbortSignal.timeout(timeoutMs), ...(init || {}) });

    api.on("before_prompt_build", async (event) => {
      const text = typeof (event && event.prompt) === "string" ? event.prompt.trim() : "";

      // 1. сообщение → MOTUS ПЕРЕД карточкой, чтобы карточка уже учла этот ход
      if (text) {
        try {
          await call("/event", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ kind: "user_message", payload: { text } }),
          });
        } catch (e) {
          log.info(`motus: /event не ушёл (${(e && e.name) || e})`);
        }
      }

      // 2. карточка (+ опц. маска инструментов)
      try {
        const r = await call("/state/card");
        if (!r.ok) return undefined;
        const d = await r.json();
        const out = {};
        const cardText = d && d.card && d.card.text;
        if (typeof cardText === "string" && cardText.trim()) out.appendContext = cardText;
        if (applyGate && d && d.gate && Array.isArray(d.gate.allowed_tools)) {
          out.toolsAllow = d.gate.allowed_tools;
        }
        return out;
      } catch (e) {
        log.info(`motus: карточка недоступна (${(e && e.name) || e}) — ход без неё`);
        return undefined;
      }
    });

    if (!applyGate) return; // без applyGate хук ниже — лишний сетевой вызов на отправку

    api.on("message_sending", async (event) => {
      const content = event && typeof event.content === "string" ? event.content : "";
      if (!content) return undefined;
      try {
        const r = await call("/state/card");
        if (!r.ok) return undefined;
        const d = await r.json();
        const gate = d && d.gate;
        if (!gate) return undefined;

        if (Array.isArray(gate.forbidden) && gate.forbidden.includes("outbound")) {
          log.info(`motus: message_sending отменено, режим ${gate.regime} запрещает outbound`);
          return { cancel: true, cancelReason: `motus: regime ${gate.regime} forbids outbound` };
        }

        if (typeof gate.max_tokens === "number" && gate.max_tokens > 0) {
          const trimmed = truncateForBudget(content, gate.max_tokens, charsPerToken);
          if (trimmed !== content) {
            log.info(`motus: message_sending обрезано под max_tokens=${gate.max_tokens} `
                     + `(${content.length} → ${trimmed.length} симв.)`);
            return { content: trimmed };
          }
        }
        return undefined;
      } catch (e) {
        log.info(`motus: message_sending без гейта (${(e && e.name) || e}) — отправка как есть`);
        return undefined;
      }
    });
  },
};
