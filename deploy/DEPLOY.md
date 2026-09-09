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
```

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
```

## Режим модели (`appraisal.mode: model`) — если понадобится

`llama-l1.service` тогда ставится **в контейнер `motus`** (не grach), модель на
бинд-маунте, `appraisal.base_url: http://127.0.0.1:8080`. Термалка и цифры —
`docs/04-model-l1.md`. По умолчанию не нужен: `mode: lexical`.
