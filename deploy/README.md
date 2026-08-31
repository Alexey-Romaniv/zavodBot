# Деплой

## Что в Google Cloud бесплатно, а что нет

Два разных механизма, их постоянно путают:

| | Срок | Что даёт |
|---|---|---|
| **Free Trial** | 90 дней | $300 кредита на любые сервисы |
| **Always Free** | бессрочно | 1× e2-micro, 30 ГБ standard-диска, 1 ГБ исходящего трафика из Северной Америки, 5 ГБ Cloud Storage |

Always Free работает и во время трайла, и после него — но **только на активном платном
биллинг-аккаунте**. То есть на 90-й день нужно нажать «Upgrade» / «Активировать полный
аккаунт»: карта привяжется, но пока укладываешься в лимиты Always Free — списаний не будет.
Если не апгрейдить, по окончании трайла ресурсы останавливают и потом удаляют.

### Подвох: внешний IPv4 в Always Free НЕ входит

Сама e2-micro бесплатна, а внешний IPv4-адрес на ней тарифицируется — $0,005/час,
это ≈ **$3,6 в месяц**. Именно на этом «бесплатный» GCP у людей превращается в счёт.
Обходится двумя способами:

* **VM без внешнего IPv4, только внешний IPv6** — Telegram по IPv6 доступен (проверено:
  `api.telegram.org` → `2001:67c:4e8:f004::9`, отвечает). IPv6-адреса не тарифицируются.
  SSH — через IAP-туннель, ему внешний IP не нужен. Рецепт ниже.
* Смириться с $3,6/мес — тогда это уже не «бесплатно», и проще взять Hetzner за €4
  без всей возни с лимитами.

**Обязательно поставь бюджетный алерт** (Billing → Budgets & alerts) на $1: любое
списание сверх Always Free ты увидишь сразу, а не в конце месяца.

### Рецепт VM без внешнего IPv4 (реальный $0)

```bash
# сеть с внешним IPv6
gcloud compute networks create zavod-net --subnet-mode=custom
gcloud compute networks subnets create zavod-subnet     --network=zavod-net --region=us-central1 --range=10.10.0.0/24     --stack-type=IPV4_IPV6 --ipv6-access-type=EXTERNAL

# SSH через IAP (внешний IP не нужен, диапазон 35.235.240.0/20 — это IAP)
gcloud compute firewall-rules create allow-iap-ssh \
    --network=zavod-net --allow=tcp:22 --source-ranges=35.235.240.0/20

# сама машина: e2-micro в бесплатном регионе, standard-диск, без внешнего IPv4
gcloud compute instances create zavodbot \
    --zone=us-central1-a --machine-type=e2-micro \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-type=pd-standard --boot-disk-size=20GB \
    --subnet=zavod-subnet --stack-type=IPV4_IPV6 --no-address

# заходим
gcloud compute ssh zavodbot --zone=us-central1-a --tunnel-through-iap
```

Дальше — шаги установки ниже. Осторожно: на IPv6-only машине `apt` иногда упирается
в зеркала без IPv6; если установка пакетов не идёт, самый простой выход — временно
дать машине внешний IPv4, всё поставить и адрес снять (за пару часов набежит меньше цента).

## Развёрнутое сейчас (для справки)

Проект `affable-cacao-507022-t2`, VM `zavodbot`, зона `us-central1-a`, e2-micro,
Debian 12, диск 20 ГБ pd-standard, сеть `zavod-net` / `zavod-subnet`,
**без внешнего IPv4**, внешний IPv6 `2600:1900:4000:570::`.

Подключение (внешнего IP нет — только через IAP):

```bash
~/google-cloud-sdk/bin/gcloud compute ssh zavodbot --zone=us-central1-a --tunnel-through-iap
```

Управление ботом на сервере:

```bash
sudo systemctl status zavodbot     # состояние
sudo systemctl restart zavodbot    # перезапуск
sudo journalctl -u zavodbot -f     # живой лог
```

## Обновление кода: автодеплой из GitHub

**Пуш в `main` = обновление бота на VM.** Всё делает
[.github/workflows/deploy.yml](../.github/workflows/deploy.yml): прогоняет тесты,
и только если они зелёные — заливает код на сервер и перезапускает бота.

```
git push origin main
        │
        ├─ Тесты (Python 3.11, как в venv на сервере)
        │
        └─ Деплой ──► вход в GCP по федерации (без ключей)
                      git archive → scp через IAP-туннель
                      deploy/update.sh на сервере
```

Почему не `git pull` на самой VM: у `github.com` нет IPv6-адресов, а машина живёт
без внешнего IPv4 — дотянуться до GitHub она не может. Поэтому деплой инициируется
снаружи: GitHub Actions заходит на VM через IAP-туннель, которому внешний IP не нужен.

### Что делает `update.sh` на сервере

Порядок выбран так, чтобы в любой момент можно было вернуться назад:

1. **бэкап базы** через `sqlite3 .backup` (консистентно даже под работающим ботом),
   в `/opt/zavodBot/backups/shifts-deploy-*.db`, хранится 14 последних;
2. **снимок текущего кода** в `/opt/zavodBot.prev`;
3. **подмена кода** — `.env`, `shifts.db`, `.venv` и `backups/` не трогаются;
4. **зависимости** — только если изменился `requirements.txt`;
5. **миграции схемы** (`python migrate.py`) — до перезапуска, чтобы поймать проблему
   на схеме, а не на упавшем боте;
6. **перезапуск и проверка**: скрипт ждёт в журнале строку «Бот запущен» — она значит,
   что токен принят и polling пошёл. `systemctl is-active` для этого недостаточно:
   systemd считает сервис живым сразу, ещё до того как бот дошёл до Telegram;
7. **откат при провале** — код возвращается из `.prev`, бот стартует на прежней версии.

Откат безопасен и после миграции: шаги схемы только добавляют таблицы и колонки,
поэтому прежний код работает с новой схемой (правило из [migrations.py](../migrations.py)).

### Ручной деплой той же логикой

Если нужно залить текущую ветку, не дожидаясь GitHub:

```bash
cd ~/Desktop/zavodBot
git archive --format=tar.gz -o /tmp/z.tar.gz HEAD
GC=~/google-cloud-sdk/bin/gcloud
$GC compute scp /tmp/z.tar.gz zavodbot:/tmp/z.tar.gz \
    --zone=us-central1-a --tunnel-through-iap
$GC compute ssh zavodbot --zone=us-central1-a --tunnel-through-iap --command='
    rm -rf /tmp/zavodbot-new && mkdir -p /tmp/zavodbot-new
    tar -xzf /tmp/z.tar.gz -C /tmp/zavodbot-new
    sudo DEPLOY_SHA=$(date +%F-manual) /tmp/zavodbot-new/deploy/update.sh /tmp/zavodbot-new'
```

`git archive HEAD` берёт только закоммиченное и только то, что в репозитории:
ни `.env`, ни базы, ни `.venv` туда не попадут в принципе.

### Что настроено в GCP (для справки)

| Что | Значение |
|---|---|
| Сервис-аккаунт | `github-deploy@affable-cacao-507022-t2.iam.gserviceaccount.com` |
| Роли в проекте | `iap.tunnelResourceAccessor`, `compute.osAdminLogin`, `compute.viewer` |
| Ещё одна роль | `iam.serviceAccountUser` — точечно на SA инстанса `598699870318-compute@developer.gserviceaccount.com`: `gcloud compute ssh/scp` без неё отвечает `PERMISSION_DENIED: ... actAs` |
| Пул федерации | `projects/598699870318/locations/global/workloadIdentityPools/github` |
| Провайдер | `.../providers/zavodbot`, условие `repository == 'Alexey-Romaniv/zavodBot'` |
| Переменные репозитория | `GCP_WIF_PROVIDER`, `GCP_SERVICE_ACCOUNT` |

Ключей сервис-аккаунта не существует — Actions получает временный токен по OIDC,
и только из этого репозитория. Красть из GitHub нечего.

Проверить, что доступ жив:

```bash
gcloud iam workload-identity-pools providers describe zavodbot \
    --location=global --workload-identity-pool=github
gh variable list --repo Alexey-Romaniv/zavodBot
```

Снятие внешнего IPv4 (уже сделано; имя конфига у GCP — `external-nat`, не «External NAT»):

```bash
~/google-cloud-sdk/bin/gcloud compute instances delete-access-config zavodbot \
    --zone=us-central1-a --access-config-name=external-nat
```

Бэкап базы: крон пользователя `zavodbot`, ежедневно в 03:00 (по UTC сервера),
в `/opt/zavodBot/backups/`, хранение 14 дней.

В `.env` на сервере стоит `SHIFTBOT_FORCE_IPV6=1`. Это обязательно для VM без внешнего
IPv4: внутренний IPv4-маршрут на машине есть, но трафик по нему проваливается в никуда,
и каждая попытка соединиться по IPv4 висит до таймаута (проверено: IPv4 — таймаут 12 с,
IPv6 — ответ за 0,36 с). Флаг убирает IPv4 из пути бота совсем.

## Вариант 1. Google Cloud e2-micro — установка на сервере

Создать VM: регион строго **us-west1 / us-central1 / us-east1**, тип **e2-micro**,
образ Debian 12, диск 10–30 ГБ standard (не SSD — он платный).

```bash
# 1) на сервере: питон и юзер под бота
sudo apt update && sudo apt install -y python3-venv sqlite3
sudo useradd -m -d /opt/zavodBot zavodbot

# 2) с локальной машины: залить код (без .venv и базы)
rsync -av --exclude '.venv' --exclude 'shifts.db' --exclude '__pycache__' \
      --exclude 'bot.log' --exclude 'backups' \
      ~/Desktop/zavodBot/ ВНЕШНИЙ_IP:/tmp/zavodBot/

# 3) на сервере: разложить и собрать окружение
sudo rsync -av /tmp/zavodBot/ /opt/zavodBot/
sudo chown -R zavodbot:zavodbot /opt/zavodBot
sudo -u zavodbot python3 -m venv /opt/zavodBot/.venv
sudo -u zavodbot /opt/zavodBot/.venv/bin/pip install -r /opt/zavodBot/requirements.txt

# 4) токен: .env через rsync не поедет, если он в .gitignore — создать вручную
sudo -u zavodbot nano /opt/zavodBot/.env    # BOT_TOKEN=...
sudo chmod 600 /opt/zavodBot/.env

# 5) автозапуск
sudo cp /opt/zavodBot/deploy/zavodbot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now zavodbot
journalctl -u zavodbot -f
```

Часовой пояс сервера не важен: `Europe/Warsaw` задан в коде явно.

Входящие порты открывать **не нужно** — бот сам ходит к Telegram (long polling),
webhook не используется.

Бэкап базы: `crontab -e -u zavodbot` → `0 3 * * * /opt/zavodBot/deploy/backup.sh`

## Вариант 2. Свой Mac (бесплатно, без карты и регистраций)

```bash
cp deploy/dev.robro.zavodbot.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.robro.zavodbot.plist
```

Бот поднимется сам после перезагрузки и после падения. Остановить:

```bash
launchctl bootout gui/$(id -u)/dev.robro.zavodbot
```

**Важно:** когда Mac спит, бот не работает. Пропущенные напоминания досылаются при
пробуждении, если смена ещё не началась — но если ноут проспал всё утро, напоминание
о смене с 06:00 придёт уже задним числом или не придёт вовсе. Для ночных смен
(22:00) это критичнее всего. Лечится в «Системных настройках → Аккумулятор»
запретом сна при подключённом питании.

## Чего избегать

- **Oracle Cloud Always Free** — щедрая ARM-машина, но простаивающие инстансы
  отбирают (95-й процентиль CPU < 20% за 7 дней). Бот простаивает почти всегда.
- **Render free** — background worker только на платном, free web service засыпает
  через 15 минут и polling ломается.
- **Railway** — $1 кредита в месяц, это несколько часов работы.
- **Heroku / Fly.io** — бесплатных тарифов для новых аккаунтов нет.
