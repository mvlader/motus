# openclaw ↔ MOTUS: контракт

**Подключение делается плагином** — `deploy/openclaw-plugin/` (там же установка).
Этот файл — справочник по HTTP-эндпоинтам, которые плагин дёргает, и по тому, что
делать с ответом.

MOTUS отвечает на `127.0.0.1:18790` внутри grach (Incus-прокси в отдельный
контейнер `motus`). Плагину видны только эти эндпоинты:

| Эндпоинт | Назначение |
|---|---|
| `GET /state/card` | карточка (текст, без чисел) + маска полномочий `gate` |
| `POST /event` | событие мира: сообщение, ошибка инструмента, потеря сети |
| `GET /task/next` | взять фоновую задачу из репертуара |
| `POST /consummation` | засчитать выполнение задачи (проверяемый факт) |
| `POST /refund` | вернуть токен, если инициация не состоялась |
| `GET /health` | жив ли |

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
  "tier": 0
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

Не ответил за 2 с → собрать промпт без карточки, ничего не ограничивать.

---

## POST /event

```
{"kind": "user_message", "payload": {"text": "<сообщение пользователя>"}}
```

MOTUS оценит текст словарём (~1 мс) и **сразу его выбросит** — хранит только 6
чисел, не содержание. Ответ разбирать не нужно, ошибку игнорировать.

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

Исполнитель: один `openclaw agent exec --isolated` (без каналов — отправить
ничего физически нельзя), результат пишется в файл, факт проверяется **кодом**
(файл создан и свеж), затем:

```
POST /consummation   {"template_id": "<из задачи>", "verified": true|false, "cost": <токены>}
POST /llm_call        {"model": "...", "purpose": "task", "tokens_in": N, "tokens_out": M, "template_id": "..."}
```

Без этого драйвы SEEKING / CARE / PLAY / FEAR / PANIC гасить нечем, кроме
разговора, и система копит активацию.

---

## Оператору (не openclaw): полное состояние

```bash
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock http://x/state/raw | python3 -m json.tool
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock 'http://x/journal/tail?n=50'
```
