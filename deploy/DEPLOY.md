# Деплой MOTUS (Raspberry Pi 5, Incus)

MOTUS живёт в **отдельном контейнере `motus`**, изолированном от openclaw
(контейнер `grach`). openclaw видит только публичный API без чисел; код и
состояние ему недоступны.

```
┌─ Pi 5 (хост) ────────────────────────────────────────────────────┐
│                                                                  │
│  ┌─ контейнер motus ──────────┐    ┌─ контейнер grach ─────────┐  │
│  │ motusd (User=motus)        │    │ openclaw (User=openclaw)  │  │
│  │  публичный:  0.0.0.0:18790 │◄───┼── 127.0.0.1:18790         │  │
│  │  админ: /run/motusd/adm.sock│   │   (Incus proxy device)    │  │
│  │  код:   /opt/motus (ro)    │    │ somatic_probe (5 мин)     │  │
│  │  var:   /var/lib/motus 700 │    │  → POST /event            │  │
│  └───────────────────────────┘    └──────────────────────────┘  │
│         ▲ openclaw НЕ достаёт: fs, adm.sock, /state/raw          │
└──────────────────────────────────────────────────────────────────┘
```

## Что где

| | |
|---|---|
| контейнер | `motus` (Debian trixie arm64), `boot.autostart=true`, снапшоты 4:00 / 7 дней |
| код | `/opt/motus`, owner `root` (motus не может переписать сам себя), пуш с хоста |
| состояние | `/var/lib/motus` (`motus:motus`, 700) — `state.json` + `journal/` |
| юнит | `deploy/motusd.service` → `/etc/systemd/system/`, `User=motus` |
| публичный API | `0.0.0.0:18790` в `motus`; Incus proxy device `motus-api` на `grach` → `127.0.0.1:18790` внутри grach |
| админ API | `/run/motusd/adm.sock` (600, motus) — только внутри контейнера `motus` |
| датчик железа | `deploy/motus-somatic.{service,timer}` внутри `grach`, `User=motus-probe`, код `/opt/motus-probe/somatic_probe.py` |
| исполнитель задач (Tier 1) | `deploy/motus-tier1.{service,timer}` внутри `grach`, `User=openclaw`, код `/opt/motus-tier1/tier1_executor.py`; дёргает `GET /task/next` → `openclaw agent exec --isolated` → `POST /consummation` |
| доставка проактива (Tier 2) | `deploy/motus-tier2.{service,timer}` внутри `grach`, `User=openclaw`, код `/opt/motus-tier2/tier2_executor.py`; дёргает `GET /initiate/pending` → `openclaw agent --deliver` → (при сбое) `POST /refund`. Требует `MOTUS_TIER2_SESSION_KEY`/`MOTUS_TIER2_TO` — без получателя не стартует |
| датчик лимита Claude | `deploy/motus-limit-probe.{service,timer}` внутри `grach`, **`User=openclaw`** (не motus-probe — нужна авторизация claude-cli), код `/opt/motus-probe/limit_probe.py`; раз в 10 мин парсит `claude -p "/usage"` → `POST /event` в MOTUS. Пороги реакции — `motus/gates.py` (`LIMIT_SOFT=0.7` мягкое урезание токенов, `LIMIT_HARD=0.95` жёсткая остановка через `before_agent_run` в плагине) |

## Развернуть с нуля

```bash
# 1. контейнер
incus launch images:debian/trixie motus            # или локальный image
incus config set motus boot.autostart=true
incus config set motus snapshots.schedule="0 4 * * *" snapshots.expiry=7d
incus exec motus -- apt-get install -y python3 ca-certificates
incus exec motus -- useradd --system --home-dir /var/lib/motus --create-home --shell /usr/sbin/nologin motus
incus exec motus -- bash -c 'mkdir -p /opt/motus /var/lib/motus && chown motus:motus /var/lib/motus && chmod 700 /var/lib/motus'

# 2. код (working tree; git clone тоже годится — тогда deploy key под user motus)
tar -C <repo> --exclude=.git --exclude=__pycache__ --exclude=var -cf - . \
  | incus exec motus -- tar -C /opt/motus -xf -
incus exec motus -- chmod -R a+rX /opt/motus
incus exec motus -- bash -c 'cd /opt/motus && python3 -m unittest discover -s tests'

# 3. сервис
incus file push deploy/motusd.service motus/etc/systemd/system/motusd.service
incus exec motus -- systemctl enable --now motusd

# 4. проброс в grach
incus config device add grach motus-api proxy \
  listen=tcp:127.0.0.1:18790 connect=tcp:$(incus list motus -c4 --format csv | grep -oE '10[0-9.]+'):18790 bind=container

# 5. проверка изнутри grach
incus exec grach -- curl -s 127.0.0.1:18790/state/card        # 200
incus exec grach -- curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:18790/state/raw  # 404

# 6. датчик железа (в grach)
incus exec grach -- useradd --system --shell /usr/sbin/nologin motus-probe
incus exec grach -- mkdir -p /opt/motus-probe
incus file push deploy/somatic_probe.py grach/opt/motus-probe/somatic_probe.py
incus exec grach -- chmod -R a+rX /opt/motus-probe
incus file push deploy/motus-somatic.service grach/etc/systemd/system/
incus file push deploy/motus-somatic.timer   grach/etc/systemd/system/
incus exec grach -- systemctl enable --now motus-somatic.timer

# 7. плагин openclaw — см. deploy/openclaw-plugin/README.md

# 8. исполнитель фоновых задач Tier 1 (в grach, от пользователя openclaw)
incus exec grach -- mkdir -p /opt/motus-tier1
incus file push deploy/tier1_executor.py grach/opt/motus-tier1/tier1_executor.py
incus exec grach -- chmod -R a+rX /opt/motus-tier1
incus file push deploy/motus-tier1.service grach/etc/systemd/system/
incus file push deploy/motus-tier1.timer   grach/etc/systemd/system/
# ПЕРВЫЙ ПРОГОН — вручную и под наблюдением (проверить, что --isolated не режет
# нужные задаче инструменты и что ход НЕ уходит ни в один канал):
incus exec grach -- sudo -u openclaw MOTUS_TIER1_STATE=/tmp/t1 \
  python3 /opt/motus-tier1/tier1_executor.py --dry-run
incus exec grach -- systemctl enable --now motus-tier1.timer
```

**Термобюджет.** Каждый прогон с задачей — полный ход openclaw. На Pi 5 под
нагрузкой это греет (в логах доходило до soft-limit 85 °C). `motus-tier1.service`
уже стоит с `Nice=15 CPUWeight=20 CPUQuota=60%`; если Pi без активного охлаждения
— поднять интервал таймера или указать лёгкую локальную модель через
`MOTUS_TIER1_MODEL`.

# 9. доставка проактивных сообщений Tier 2 (в grach, от пользователя openclaw)
incus exec grach -- mkdir -p /opt/motus-tier2
incus file push deploy/tier2_executor.py grach/opt/motus-tier2/tier2_executor.py
incus exec grach -- chmod -R a+rX /opt/motus-tier2
incus file push deploy/motus-tier2.service grach/etc/systemd/system/
incus file push deploy/motus-tier2.timer   grach/etc/systemd/system/
# ОБЯЗАТЕЛЬНО указать получателя — без него исполнитель откажется стартовать:
incus exec grach -- mkdir -p /etc/systemd/system/motus-tier2.service.d
incus exec grach -- sh -c 'cat > /etc/systemd/system/motus-tier2.service.d/override.conf <<EOF
[Service]
Environment=MOTUS_TIER2_SESSION_KEY=agent:main:telegram:direct:<id>
EOF'

# 10. датчик лимита Claude (в grach, от пользователя openclaw — нужен claude-cli login)
incus file push deploy/limit_probe.py grach/opt/motus-probe/limit_probe.py
incus exec grach -- chmod a+rX /opt/motus-probe/limit_probe.py
incus file push deploy/motus-limit-probe.service grach/etc/systemd/system/
incus file push deploy/motus-limit-probe.timer   grach/etc/systemd/system/
# первый прогон вручную — печатает payload, ничего не шлёт:
incus exec grach -- sudo -u openclaw python3 /opt/motus-probe/limit_probe.py --dry-run
incus exec grach -- systemctl daemon-reload
incus exec grach -- systemctl enable --now motus-limit-probe.timer
incus exec grach -- systemctl daemon-reload
# первый прогон — вручную, --dry-run печатает промпт и НЕ отправляет:
incus exec grach -- sudo -u openclaw MOTUS_TIER2_STATE=/tmp/t2 MOTUS_TIER2_SESSION_KEY=agent:main:telegram:direct:<id> \
  python3 /opt/motus-tier2/tier2_executor.py --dry-run
# И только когда готов реально получать проактивные сообщения:
# budget.initiation_enabled: true в config/default.json (сейчас false) + рестарт motusd
incus exec grach -- systemctl enable --now motus-tier2.timer

## Обновить код

```bash
tar -C <repo> --exclude=.git --exclude=__pycache__ --exclude=var -cf - . \
  | incus exec motus -- tar -C /opt/motus -xf -
incus exec motus -- bash -c 'chmod -R a+rX /opt/motus && systemctl restart motusd'
```

Если IP контейнера `motus` сменился (редко) — поправить `connect=` у device
`motus-api` на grach.

## Инспекция состояния (для оператора)

```bash
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock http://x/state/raw | python3 -m json.tool
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock 'http://x/journal/tail?n=50'
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock -XPOST http://x/sleep -d '{"force":true}'

# Tier 1: что делал исполнитель
incus exec grach -- journalctl -u motus-tier1.service --since today
incus exec grach -- ls -lt /var/lib/motus-tier1/drops/     # результаты фоновых задач

# Tier 2: доставлялись ли проактивные сообщения
incus exec grach -- journalctl -u motus-tier2.service --since today
```

## L-1: модель, а не словарь (`appraisal.mode: model`, умолчание с 2026-09-10)

По умолчанию `motusd` (в контейнере `motus`) на каждое текстовое событие ходит
по LAN в ollama на ПК `192.168.2.27:11434` (`ge4b-heretic:latest`) — контейнер
`motus` должен иметь сетевой доступ к этому адресу (проверить:
`incus exec motus -- curl -s http://192.168.2.27:11434/api/tags`). Обоснование
выбора модели, риски (зависимость от ПК, латентность) и live-бенчмарк —
`docs/04-model-l1.md`.

Альтернатива без сети — `llama-l1.service` **в контейнере `motus`** (не grach) с
GGUF на бинд-маунте, `appraisal.api: llamacpp`, `base_url: http://127.0.0.1:8080`.
На замерах она хуже (Qwen3-1.7B на CPU Pi проигрывает и словарю, и gemma4 на ПК),
держим как запасной вариант на случай, если ПК окажется недоступен слишком часто.
