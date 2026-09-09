# Плагин openclaw → MOTUS

openclaw подключается к MOTUS **плагином** (не ручными curl, не текстом в чат).
У openclaw есть типизированные хуки; плагин вешается на один — `before_prompt_build`,
который срабатывает перед каждым ходом агента.

| шаг в хуке | что делает |
|---|---|
| 1 | `POST /event` с текстом сообщения пользователя (`event.prompt`) |
| 2 | `GET /state/card` → `card.text` дописывается в конец промпта (`appendContext`) |
| 3 | при `config.applyGate: true` — `gate.allowed_tools` сужает инструменты хода (`toolsAllow`) |

MOTUS не ответил за `timeoutMs` (2 с) → ход идёт без карточки. Плагин видит только
публичный API — ни чисел состояния, ни журнала.

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
#    plugins.entries.motus.config = { applyGate: false }
#    (без allow* хук before_prompt_build не вызывается)

# 4. перезапустить gateway
incus exec grach -- bash -c 'sudo -u openclaw XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway'
```

Проверка:

```bash
incus exec grach -- su - openclaw -c '~/.npm-global/bin/openclaw plugins inspect motus --runtime'
#   Status: loaded / Typed hooks: before_prompt_build / allow* : true

# прогнать ход и убедиться, что событие дошло:
incus exec grach -- su - openclaw -c '~/.npm-global/bin/openclaw agent -m "спасибо, нашёл отличную идею"'
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock 'http://x/journal/tail?n=6'
#   → impulse msg:novelty / msg:warmth / msg:positive
```

## Порядок внедрения

1. `applyGate: false` (как сейчас). Карточка в промпте, сообщения уходят в MOTUS,
   инструменты не режутся. Пару дней смотреть журнал — состояние движется осмысленно?
2. `applyGate: true` — MOTUS начинает сужать инструменты под режим (`RAGE` → только
   `read`, и т.п.).

## Чего плагин пока НЕ делает (следующая фаза)

- Не блокирует проактивные сообщения при `may_initiate: false` (нужен хук
  `message_sending` / `before_agent_reply` с `eligibleTriggers:["cron","heartbeat"]`).
- Не применяет `max_tokens`, `forbidden`.
- Не берёт фоновые задачи (`GET /task/next` → выполнить без отправки наружу →
  `POST /consummation`).

## Совместимость

Хуки openclaw помечены experimental. Проверено на openclaw `2026.9.3`. Событие
шлётся из `event.prompt` — для канальных сообщений там может быть немного
channel-обвязки, MOTUS всё равно хранит только 6 чисел, не текст.
