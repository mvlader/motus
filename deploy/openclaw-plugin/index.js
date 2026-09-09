// Мост openclaw → MOTUS. Формат — как у остальных плагинов openclaw
// (~/.openclaw/extensions/<id>/index.js, плоский объект с register(api)).
//
// Один хук — before_prompt_build, срабатывает перед каждым ходом агента:
//   1. отдаёт текст сообщения пользователя в MOTUS (POST /event);
//   2. тянет свежую карточку (GET /state/card) и дописывает её в конец промпта;
//   3. при config.applyGate=true сужает набор инструментов хода до gate.allowed_tools.
//
// MOTUS не ответил за timeoutMs → ход идёт как обычно, без карточки. Плагин видит
// только публичный API (5 эндпоинтов): ни чисел состояния, ни журнала.

export default {
  id: "motus",
  name: "MOTUS",
  register(api) {
    const cfg = api.pluginConfig || {};
    const base = (cfg.url || "http://127.0.0.1:18790").replace(/\/$/, "");
    const timeoutMs = cfg.timeoutMs || 2000;
    const applyGate = cfg.applyGate === true;
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
  },
};
