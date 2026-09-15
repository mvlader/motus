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
│  │  var:   /var/lib/motus 700 │    │ limit_probe (10 мин)      │  │
│  │  L-1 / курирование:        │    │ tier1 (15 мин), tier2 (10)│  │
│  │   /opt/claude/claude -p ───┼──► api.anthropic.com         │  │
│  └───────────────────────────┘    └──────────────────────────┘  │
│         ▲ openclaw НЕ достаёт: fs, adm.sock, /state/raw          │
└──────────────────────────────────────────────────────────────────┘
```

## Что где

| | |
|---|---|
| контейнер | `motus` (Debian trixie arm64), `boot.autostart=true`, снапшоты 4:00 / 7 дней, часовой пояс America/Toronto |
| код | `/opt/motus`, owner `root` (motus не может переписать сам себя), пуш с хоста |
| состояние | `/var/lib/motus` (`motus:motus`, 700) — `state.json` + `journal/` |
| юнит | `deploy/motusd.service` → `/etc/systemd/system/`, `User=motus` |
| L-1 и самокурирование | `claude-sonnet-5` через `/opt/claude/claude -p` (подписка Claude Code), токен `CLAUDE_CODE_OAUTH_TOKEN` в `/etc/motus/claude.env` (600 root, `EnvironmentFile` юнита); без токена — словарный откат |
| публичный API | `0.0.0.0:18790` в `motus`; Incus proxy device `motus-api` на `grach` → `127.0.0.1:18790` внутри grach |
| админ API | `/run/motusd/adm.sock` (600, motus) — только внутри контейнера `motus` |
| датчик железа | `deploy/motus-somatic.{service,timer}` внутри `grach`, `User=motus-probe`, код `/opt/motus-probe/somatic_probe.py` |
| исполнитель задач (Tier 1) | `deploy/motus-tier1.{service,timer}` внутри `grach`, `User=openclaw`, код `/opt/motus-tier1/tier1_executor.py`; дёргает `GET /task/next` → основной путь `claude -p` (`MOTUS_TIER1_CLAUDE_MODEL=claude-sonnet-5`, `--tools Read,Glob,Grep,Write,Edit`, вход — подписка из `~openclaw/.claude`); при отказе Claude или `limit_block` — `openclaw agent exec --config /opt/motus-tier1/exec-config.json` (`ge4b-heretic` на ПК, read/write/edit/exec/memory, без каналов и веба) → `POST /consummation`. Через openclaw с рантаймом claude-cli ходить нельзя: там список инструментов не применяется. Результаты — `~openclaw/.openclaw/workspace/.motus/drops/` |
| доставка проактива (Tier 2) | `deploy/motus-tier2.{service,timer}` внутри `grach`, `User=openclaw`, код `/opt/motus-tier2/tier2_executor.py`; дёргает `GET /initiate/pending` → `openclaw agent --deliver` → (при сбое) `POST /refund`. Требует `MOTUS_TIER2_SESSION_KEY`/`MOTUS_TIER2_TO` — без получателя не стартует |
| датчик лимита Claude | `deploy/motus-limit-probe.{service,timer}` внутри `grach`, **`User=openclaw`** (не motus-probe — нужна авторизация claude-cli), код `/opt/motus-probe/limit_probe.py`; раз в 10 мин парсит `claude -p "/usage"` → `POST /event` в MOTUS. Пороги реакции — `motus/gates.py` (`LIMIT_SOFT=0.7` мягкое урезание токенов, `LIMIT_HARD=0.95` жёсткая остановка через `before_agent_run` в плагине) |
| конфигуратор | `tools/motusctl.py` на хосте с репозиторием; правит `config/` в git и выкладывает в `motus` через `incus exec` |

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

# 3. часовой пояс оператора (иначе контейнер живёт в UTC: журнал и логи — по UTC)
incus exec motus -- timedatectl set-timezone America/Toronto

# 4. Claude для L-1 и самокурирования — см. раздел «Claude» ниже (бинарь + токен)

# 5. сервис
incus file push deploy/motusd.service motus/etc/systemd/system/motusd.service
incus exec motus -- systemctl enable --now motusd

# 6. проброс в grach
incus config device add grach motus-api proxy \
  listen=tcp:127.0.0.1:18790 connect=tcp:$(incus list motus -c4 --format csv | grep -oE '10[0-9.]+'):18790 bind=container

# 7. проверка изнутри grach
incus exec grach -- curl -s 127.0.0.1:18790/state/card        # 200
incus exec grach -- curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:18790/state/raw  # 404

# 8. датчик железа (в grach)
incus exec grach -- useradd --system --shell /usr/sbin/nologin motus-probe
incus exec grach -- mkdir -p /opt/motus-probe
incus file push deploy/somatic_probe.py grach/opt/motus-probe/somatic_probe.py
incus exec grach -- chmod -R a+rX /opt/motus-probe
incus file push deploy/motus-somatic.service grach/etc/systemd/system/
incus file push deploy/motus-somatic.timer   grach/etc/systemd/system/
incus exec grach -- systemctl enable --now motus-somatic.timer

# 9. плагин openclaw — см. deploy/openclaw-plugin/README.md

# 10. исполнитель фоновых задач Tier 1 (в grach, от пользователя openclaw)
incus exec grach -- mkdir -p /opt/motus-tier1
incus file push deploy/tier1_executor.py grach/opt/motus-tier1/tier1_executor.py
incus exec grach -- chmod -R a+rX /opt/motus-tier1
incus file push deploy/motus-tier1.service grach/etc/systemd/system/
incus file push deploy/motus-tier1.timer   grach/etc/systemd/system/
incus file push deploy/tier1-exec-config.json grach/opt/motus-tier1/exec-config.json --uid 1000 --gid 1000 --mode 0644
# ПЕРВЫЙ ПРОГОН — вручную и под наблюдением (проверить, что exec-конфиг не режет
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

```bash
# 11. доставка проактивных сообщений Tier 2 (в grach, от пользователя openclaw)
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
incus exec grach -- systemctl daemon-reload
# первый прогон — вручную, --dry-run печатает промпт и НЕ отправляет:
incus exec grach -- sudo -u openclaw MOTUS_TIER2_STATE=/tmp/t2 MOTUS_TIER2_SESSION_KEY=agent:main:telegram:direct:<id> \
  python3 /opt/motus-tier2/tier2_executor.py --dry-run
incus exec grach -- systemctl enable --now motus-tier2.timer
# budget.initiation_enabled у оператора true (с 2026-09-12); выключить — false + рестарт motusd

# 12. датчик лимита Claude (в grach, от пользователя openclaw — нужен claude-cli login)
incus file push deploy/limit_probe.py grach/opt/motus-probe/limit_probe.py
incus exec grach -- chmod a+rX /opt/motus-probe/limit_probe.py
incus file push deploy/motus-limit-probe.service grach/etc/systemd/system/
incus file push deploy/motus-limit-probe.timer   grach/etc/systemd/system/
# первый прогон вручную — печатает payload, ничего не шлёт:
incus exec grach -- sudo -u openclaw python3 /opt/motus-probe/limit_probe.py --dry-run
incus exec grach -- systemctl daemon-reload
incus exec grach -- systemctl enable --now motus-limit-probe.timer
```

## Обновить код

```bash
tar -C <repo> --exclude=.git --exclude=__pycache__ --exclude=var -cf - . \
  | incus exec motus -- tar -C /opt/motus -xf -
incus exec motus -- bash -c 'chmod -R a+rX /opt/motus && cd /opt/motus && python3 -m unittest discover -s tests'
incus exec motus -- systemctl restart motusd
```

Меняется только конфиг — проще через конфигуратор `python3 tools/motusctl.py`:
он сверит деплой с git, сделает бэкап, выложит файл и предложит рестарт.
Юнит изменился — `incus file push deploy/motusd.service ...` и `systemctl daemon-reload`.

Если IP контейнера `motus` сменился (редко) — поправить `connect=` у device
`motus-api` на grach.

## Инспекция состояния (для оператора)

```bash
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock http://x/state/raw | python3 -m json.tool
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock 'http://x/journal/tail?n=50'
incus exec motus -- curl -s --unix-socket /run/motusd/adm.sock -XPOST http://x/sleep -d '{"force":true}'

# Tier 1: что делал исполнитель
incus exec grach -- journalctl -u motus-tier1.service --since today
incus exec grach -- ls -lt /home/openclaw/.openclaw/workspace/.motus/drops/   # результаты фоновых задач

# Tier 2: доставлялись ли проактивные сообщения
incus exec grach -- journalctl -u motus-tier2.service --since today
```

## Claude для L-1 и самокурирования

`appraisal.api` и `curation.api` = `claude_cli`: модель `claude-sonnet-5` через
`claude -p` на подписке Claude Code, без инструментов и со своим коротким системным
промптом (~1 тыс. токенов на сообщение — расход общего 5-часового лимита).

```bash
# бинарь (нативный, зависит только от libc) — из того же npm-пакета, что в grach
incus exec grach -- cat /home/openclaw/.npm-global/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe \
  | incus exec motus -- sh -c 'mkdir -p /opt/claude && cat > /opt/claude/claude && chmod 755 /opt/claude/claude'

# долгоживущий токен подписки: `claude setup-token` в любом терминале с браузером,
# затем вставить токен — в НАСТОЯЩЕМ терминале (-t; кнопка Run без stdin не подойдёт):
incus exec -t motus -- sh -c 'umask 077; mkdir -p /etc/motus; printf "токен: "; read -r t; \
  printf "CLAUDE_CODE_OAUTH_TOKEN=%s\n" "$t" > /etc/motus/claude.env'
incus exec motus -- systemctl restart motusd

# проверка: переменная дошла до демона (значение не печатается)
incus exec motus -- sh -c 'tr "\0" "\n" < /proc/$(systemctl show motusd -p MainPID --value)/environ | grep -o "^CLAUDE_CODE_OAUTH_TOKEN="'
```

Строка в файле — именно `CLAUDE_CODE_OAUTH_TOKEN=<токен>`: голый токен без имени
переменной systemd молча пропускает, и L-1 работает на словаре. Юнит запускает CLI
с `HOME=/var/lib/motus`, `DISABLE_AUTOUPDATER=1` и разрешённым IPv6
(`api.anthropic.com` отдаёт и v6-адреса); `PrivateTmp=yes` обязателен — CLI пишет
во временный каталог.

Нужен другой рантайм (ollama на ПК, llama.cpp в контейнере) — `appraisal.api` в
конфиге, подробности в `docs/04-model-l1.md`.

Отдельный токен, а не копия `~/.claude/.credentials.json` из grach: два экземпляра
одного OAuth-входа сбивают друг другу обновление токена.

