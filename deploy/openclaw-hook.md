# openclaw ↔ MOTUS: контракт

**Три потребителя публичного API** — плагин `deploy/openclaw-plugin/` (живой ход),
`deploy/tier1_executor.py` (фоновые задачи) и `deploy/tier2_executor.py`
(проактивные сообщения). Этот файл — справочник по эндпоинтам и тому, что делать
с ответом.

MOTUS отвечает на `127.0.0.1:18790` внутри grach (Incus-прокси в отдельный
контейнер `motus`). Наружу видны только эти эндпоинты:

| Эндпоинт | Кто дёргает | Назначение |
|---|---|---|
| `GET /state/card` | плагин | карточка (текст, без чисел) + маска полномочий `gate` + `limit_block` |
| `POST /event` | плагин | событие мира: сообщение, ошибка инструмента, потеря сети |
| `GET /task/next` | tier1_executor | взять фоновую задачу из репертуара |
| `POST /consummation` | tier1_executor | засчитать выполнение задачи (проверяемый факт) |
| `GET /initiate/pending` | tier2_executor | есть ли решённая проактивная инициация к доставке |
| `POST /refund` | tier1_executor, tier2_executor | вернуть токен, если инициация/задача не состоялась |
| `POST /llm_call` | tier1_executor | зарегистрировать вызов модели (токены, шаблон) |
| `GET /health` | любой | жив ли |

`GET /state/raw`, журнал, `/tick`, `/sleep` → **404**. Числа драйвов, код, файл
состояния — в другом контейнере, недоступны.

---

## GET /state/card

```json
{
  "card": { "text": "последний обмен — несколько минут назад; нить свежая. Состояние: ровное рабочее состояние. Отвечай по существу." },
  "gate": {
    "regime": "baseline",
    "may_initiate": false,
    "max_tokens": 900,
    "allowed_tools": ["read", "memory", "exec", "write", "net", "outbound"],
    "forbidden": [],
    "context_band": "fresh"
  },
  "tier": 0,
  "limit_block": { "active": false, "message": null, "resume_at": null }
}
```

Что делать с ответом:

1. `card.text` → вставить **как есть** в конец системного промпта.
2. `gate` → ограничить ход:

| поле | значение | действие |
|---|---|---|
| `may_initiate` | `false` | не давать модели писать пользователю по своей инициативе |
| `allowed_tools` | список | оставить ходу только эти инструменты |
| `forbidden` | метки | не выполнять действия этих типов: `outbound` (отправка), `irreversible`, `write`, `net`, `new_topics`, `long_form`, `promises` |
| `max_tokens` | число | верхний предел длины ответа |
| `regime` | имя | справочно: `baseline` / `SEEKING` / `CARE` / `PLAY` / `FEAR` / `RAGE` / `PANIC` |

3. `limit_block.active: true` (лимит Claude ≥ 95 %) → не запускать ход вовсе,
   ответить пользователю `limit_block.message` — готовым текстом «отвечу снова
   примерно через N мин», посчитанным кодом MOTUS. Так делает хук плагина
   `before_agent_run`.

Не ответил за `timeoutMs` плагина → собрать промпт без карточки, ничего не
ограничивать (fail open).

---

## POST /event

```
{"kind": "user_message", "payload": {"text": "<сообщение пользователя>"}}
```

MOTUS оценит текст моделью L-1 (сейчас `claude-sonnet-5`, ~2–3 с; при отказе —
словарём, ~1 мс) и **сразу его выбросит** — хранит только 6 чисел, не содержание.
Ответ приходит после оценки, поэтому `timeoutMs` плагина должен быть больше
`appraisal.timeout_s` (55 с). Ответ разбирать не нужно, ошибку игнорировать.

Другие полезные события:

| Что случилось | тело |
|---|---|
| инструмент упал | `{"kind":"tool_error","payload":{"tool":"имя","blocking":true}}` |
| пропала сеть | `{"kind":"net_down"}` / вернулась: `{"kind":"net_up"}` |
| бот ответил (двигает часы контекста) | `{"kind":"assistant_message"}` |

Альтернатива — прислать готовую оценку самому:
`{"kind":"user_message","payload":{"appraisal":{"novelty":1,"social_warmth":1}}}`.
Если `appraisal` есть — `text` игнорируется.

---

## Фоновые задачи (Tier 1)

Это делает **не плагин**, а отдельный исполнитель `deploy/tier1_executor.py`
(таймер `motus-tier1` в grach, `User=openclaw`). Плагину здесь делать нечего: он
работает только когда есть живой ход, а задачи нужны в тишине.

```
GET /task/next   →   {"task": {"template_id": "...", "allowed_tools": [...],
                               "consummation": {"type": "..."}, ...} | null, "tier": 1}
```

Исполнитель: один `openclaw agent exec --config exec-config.json` — узкий набор
инструментов (read/write/edit/exec/memory), без каналов отправки и без веба;
результат пишется в файл, факт проверяется **кодом** (файл создан и свеж; для
`reflection` — что ход состоялся), затем:

```
POST /consummation   {"template_id": "<из задачи>", "verified": true|false, "cost": <токены>}
POST /llm_call        {"model": "...", "purpose": "task", "tokens_in": N, "tokens_out": M, "template_id": "..."}
```

Без этого драйвы SEEKING / CARE / PLAY / FEAR / PANIC гасить нечем, кроме
разговора, и система копит активацию.

---

## Проактивные сообщения (Tier 2)

Тоже не плагин — `deploy/tier2_executor.py` (таймер `motus-tier2`, каждые 10 мин).
`budget.initiation_enabled: true` у оператора с 2026-09-12 (канал доставки
проверен). В тихие часы (`budget.quiet_hours`) движок инициацию не решает вовсе.

```
GET /initiate/pending   →   {"pending": false}
                         |   {"pending": true, "card": {...}, "gate": {"regime", "max_tokens", "forbidden"}}
```

`pending: true` только когда движок УЖЕ решил инициировать (внутри обычного
тика — здесь ничего не тикает и не списывает) И тишина дольше
`INITIATE_MIN_SILENCE_S` (5 мин, в `daemon.py`) — иначе тик, вызванный
обычным `/state/card` посреди живого разговора, читался бы как повод написать
поверх активного диалога.

Исполнитель не шлёт `card.text` как есть — просит модель ходом через реальный
гейтвей (`openclaw agent --deliver`) написать что-то в рамках карточки; текст
сообщения выбирает модель, не скрипт. Успех подтверждать не нужно:
`initiation_pending` снимается либо ответом пользователя (`user_message`),
либо таймаутом (`budget.unanswered_after_s`) — то и другое уже в движке.
Только на сбое канала — `POST /refund`, иначе токен бюджета списан впустую.

`message_sending`-хук плагина (при `applyGate: true`) — независимая вторая
проверка на самой отправке: если к моменту доставки гейт успел смениться на
`forbidden: outbound` (сейчас это только RAGE), отправка всё равно будет отменена.

---

## Оператору (не openclaw): полное состояние

```bash
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock http://x/state/raw | python3 -m json.tool
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock 'http://x/journal/tail?n=50'
```
