# Плагин openclaw → MOTUS

openclaw подключается к MOTUS **плагином** (не ручными curl, не текстом в чат).
Три типизированных хука:

**`before_agent_run`** — до вызова модели вообще, независимо от `applyGate`: если
лимит Claude почти исчерпан (`limit_block.active` в `/state/card`, порог
`LIMIT_HARD = 0.95` в `motus/gates.py`), ход блокируется и пользователь получает
готовый текст из MOTUS («отвечу снова примерно через N мин») — лимит не тратится
на попытку, которая всё равно не пройдёт.

**`before_prompt_build`** — перед каждым ходом агента:

| шаг | что делает |
|---|---|
| 1 | `POST /event` с текстом сообщения пользователя (`event.prompt`) |
| 2 | `GET /state/card` → `card.text` дописывается в конец промпта (`appendContext`) |
| 3 | при `config.applyGate: true` — `gate.allowed_tools` сужает инструменты хода (`toolsAllow`) |

**`message_sending`** — прямо перед отправкой готового текста в канал, только при
`config.applyGate: true`:

| условие | действие |
|---|---|
| `gate.forbidden` содержит `outbound` (сейчас только режим RAGE) | отправка отменяется целиком (`cancel: true`) — любая, включая ответ на прямой вопрос |
| иначе `content.length` больше бюджета под `gate.max_tokens` | текст обрезается по границе слова (`content: ...`) — оценка символов на токен грубая (`charsPerToken`, умолч. 2.6), настоящего токенайзера у плагина нет |

Это единственное место, где ограничения гейта применяются ЖЁСТКО, а не просьбой
в карточке: директиву модель может не заметить, а `message_sending` — последняя
точка перед реальной отправкой.

MOTUS не ответил за `timeoutMs` → ход идёт без карточки / отправка не
блокируется (fail open — молчание MOTUS не должно ронять доставку). Плагин видит
только публичный API — ни чисел состояния, ни журнала.

**`timeoutMs` подбирать под `appraisal.mode`.** Дефолт в коде плагина — 2000 мс,
верно только для `mode: lexical` (без сети, ~0 мс). При `mode: model` `/event` ждёт
оценку L-1: сейчас это `claude-sonnet-5` через `claude -p`, ~2–3 с, с потолком
`appraisal.timeout_s` = 55 с (docs/04-model-l1.md). Слишком короткий `timeoutMs` —
клиент отваливается раньше ответа сервера (`BrokenPipeError` в журнале MOTUS,
безвредно для состояния, но карточка в этот ход не попадает в промпт). Ставить
заметно больше `appraisal.timeout_s`, у оператора 60000:
`openclaw config set plugins.entries.motus.config.timeoutMs 60000` — или через
конфигуратор, раздел «OpenClaw».

## Файлы

- `index.js` — сам плагин (плоский объект `{ id, register(api) }`, как остальные плагины openclaw).
- `openclaw.plugin.json` — манифест (id + configSchema).
- `package.json` — `openclaw.extensions: ["./index.js"]`.

## Установка (внутри контейнера grach, от пользователя openclaw)

```bash
# 1. положить каталог, напр. в ~/motus-src
incus file push -r deploy/openclaw-plugin grach/home/openclaw/motus-src
incus exec grach -- su - openclaw -c 'chmod -R a+rX ~/motus-src'

# 2. поставить + включить (--accept-capabilities: плагин ходит по сети)
incus exec grach -- su - openclaw -c '~/.npm-global/bin/openclaw plugins install --link ~/motus-src --force --accept-capabilities --acknowledge-install-policy-warning'
incus exec grach -- su - openclaw -c '~/.npm-global/bin/openclaw plugins enable motus --accept-capabilities'

# 3. права хуков в ~/.openclaw/openclaw.json
#    plugins.entries.motus.hooks = { allowConversationAccess: true, allowPromptInjection: true }
#    plugins.entries.motus.config = { applyGate: false, timeoutMs: 60000 }
#    (без allow* хук before_prompt_build не вызывается; про timeoutMs см. выше)

# 4. перезапустить gateway
incus exec grach -- bash -c 'sudo -u openclaw XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway'
```

Проверка:

```bash
incus exec grach -- su - openclaw -c '~/.npm-global/bin/openclaw plugins inspect motus --runtime'
#   Status: loaded / Typed hooks: before_agent_run, before_prompt_build, message_sending / allow* : true

# прогнать ход и убедиться, что событие дошло:
incus exec grach -- su - openclaw -c '~/.npm-global/bin/openclaw agent -m "спасибо, нашёл отличную идею"'
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock 'http://x/journal/tail?n=6'
#   → impulse msg:novelty / msg:warmth / msg:positive
```

## Порядок внедрения

1. `applyGate: false` (у оператора сейчас так). Карточка в промпте, сообщения уходят
   в MOTUS, блок хода на исчерпанном лимите работает, инструменты не режутся.
   Смотреть журнал — состояние движется осмысленно?
2. `applyGate: true` — MOTUS начинает сужать инструменты под режим (`RAGE` → только
   `read`, и т.п.).

## Чего плагин пока НЕ делает

- Фоновые задачи (Tier 1) и доставка проактивных сообщений (Tier 2) — это НЕ
  этот плагин: отдельные исполнители на таймерах,
  `deploy/tier1_executor.py` / `deploy/tier2_executor.py` (обоснование — в их
  докстрингах: задачи нужны в тишине, плагин работает только на живом ходе).
- `forbidden` кроме `outbound` (`irreversible`, `new_topics`, `long_form`,
  `promises`) — это по-прежнему просьба текстом в карточке (L2 design:
  директивы — не механический запрет, модель либо следует, либо нет), не
  хук. Механически жёстко enforced только `allowed_tools`, `outbound` и
  `max_tokens` (через `message_sending`).

## Совместимость

Хуки openclaw помечены experimental. Проверено на openclaw `2026.9.3`. Событие
шлётся из `event.prompt` — для канальных сообщений там может быть немного
channel-обвязки, MOTUS всё равно хранит только 6 чисел, не текст.
