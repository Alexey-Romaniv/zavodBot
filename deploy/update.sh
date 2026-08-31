#!/usr/bin/env bash
# Обновление бота на сервере. Запускается из-под root (sudo) с новым кодом на входе:
#
#   sudo /tmp/zavodbot-new/deploy/update.sh /tmp/zavodbot-new
#
# Скрипт всегда берётся из НОВОГО кода — значит правки самого деплоя применяются
# сразу же, а не со следующего раза.
#
# Порядок такой, чтобы в любой момент можно было вернуться назад:
#   1. бэкап базы;
#   2. снимок текущего кода;
#   3. подмена кода (база, .env и .venv не трогаются);
#   4. зависимости, потом миграции схемы;
#   5. перезапуск и проверка, что бот действительно поднялся;
#   6. если не поднялся — код откатывается, бот стартует на прежней версии.
#
# Откат безопасен и после миграций: они только добавляют таблицы и колонки,
# поэтому прежний код продолжает работать с новой схемой (см. migrations.py).
set -euo pipefail

SRC="${1:-/tmp/zavodbot-new}"
APP="${ZAVODBOT_APP:-/opt/zavodBot}"
PREV="${APP}.prev"
OWNER="${ZAVODBOT_USER:-zavodbot}"
SERVICE="${ZAVODBOT_SERVICE:-zavodbot}"
KEEP_BACKUPS=14
HEALTH_TIMEOUT=45

step() { printf '\n▶ %s\n' "$*"; }
fail() { printf '\n✖ %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "нужен root: запускай через sudo"
[[ -d $SRC ]] || fail "нет каталога с новым кодом: $SRC"
[[ -f $SRC/bot.py && -f $SRC/requirements.txt ]] || fail "в $SRC не похоже на код бота"
[[ -d $APP ]] || fail "бот не установлен в $APP — первую установку делай по deploy/README.md"
[[ -f $APP/.env ]] || fail "нет $APP/.env — без токена бот не поднимется"

PY="$APP/.venv/bin/python"
PIP="$APP/.venv/bin/pip"
[[ -x $PY ]] || fail "нет виртуального окружения $APP/.venv"

# --- 1. Бэкап базы -------------------------------------------------------
# Отдельно от бэкапа внутри миграций: тот делается только когда схема меняется,
# а перед деплоем копия нужна всегда.
DB="$APP/shifts.db"
if [[ -f $DB ]]; then
    step "Бэкап базы"
    install -d -o "$OWNER" -g "$OWNER" "$APP/backups"
    SNAPSHOT="$APP/backups/shifts-deploy-$(date +%Y%m%d-%H%M%S).db"
    # .backup, а не cp: копия консистентна, даже если бот прямо сейчас пишет
    sudo -u "$OWNER" sqlite3 "$DB" ".backup '$SNAPSHOT'"
    echo "  $SNAPSHOT ($(du -h "$SNAPSHOT" | cut -f1))"
    # старые копии чистим, чтобы 30 ГБ диска не кончились незаметно
    ls -1t "$APP"/backups/shifts-deploy-*.db 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) \
        | xargs -r rm -f
else
    step "Базы ещё нет — бэкапить нечего"
fi

# --- 2. Снимок текущего кода --------------------------------------------
step "Снимок текущей версии в $PREV"
rm -rf "$PREV"
mkdir -p "$PREV"
rsync -a --delete \
    --exclude '.venv/' --exclude '.env' --exclude 'shifts.db' \
    --exclude 'backups/' --exclude '__pycache__/' --exclude '*.log' \
    "$APP/" "$PREV/"
OLD_VERSION="$(cd "$APP" && cat .deployed-sha 2>/dev/null || echo "неизвестно")"
echo "  было: $OLD_VERSION"

# --- 3. Новый код --------------------------------------------------------
step "Раскладываю новый код в $APP"
rsync -a --delete \
    --exclude '.venv/' --exclude '.env' --exclude 'shifts.db' \
    --exclude 'backups/' --exclude '__pycache__/' --exclude '*.log' \
    --exclude '.deployed-sha' \
    "$SRC/" "$APP/"
[[ -n "${DEPLOY_SHA:-}" ]] && echo "$DEPLOY_SHA" > "$APP/.deployed-sha"
chown -R "$OWNER:$OWNER" "$APP"
# rsync -a переносит на приёмник и права каталога-источника, а деплой распаковывает
# код во временный каталог с правами 700 — задаём права явно, чтобы каталог
# приложения не зависел от того, откуда приехал код.
chmod 755 "$APP"
chmod 600 "$APP/.env"
chmod +x "$APP/deploy"/*.sh 2>/dev/null || true

rollback() {
    printf '\n↩ Откатываю код на прежнюю версию\n' >&2
    rsync -a --delete \
        --exclude '.venv/' --exclude '.env' --exclude 'shifts.db' \
        --exclude 'backups/' --exclude '__pycache__/' --exclude '*.log' \
        "$PREV/" "$APP/"
    chown -R "$OWNER:$OWNER" "$APP"
    systemctl restart "$SERVICE" || true
    sleep 5
    systemctl is-active --quiet "$SERVICE" \
        && printf '↩ Бот работает на прежней версии\n' >&2 \
        || printf '✖ Прежняя версия тоже не поднялась — смотри journalctl -u %s\n' "$SERVICE" >&2
}

# --- 4. Зависимости и схема ---------------------------------------------
if ! cmp -s "$SRC/requirements.txt" "$PREV/requirements.txt"; then
    step "requirements.txt изменился — обновляю зависимости"
    sudo -u "$OWNER" "$PIP" install --quiet --upgrade -r "$APP/requirements.txt" \
        || { rollback; fail "не удалось поставить зависимости"; }
else
    step "Зависимости без изменений"
fi

step "Миграции схемы"
if ! sudo -u "$OWNER" "$PY" "$APP/migrate.py"; then
    rollback
    fail "миграции не применились — база осталась в прежнем состоянии, код откатан"
fi

# --- 5. Перезапуск и проверка -------------------------------------------
step "Перезапуск $SERVICE"
STARTED="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl restart "$SERVICE"

# Ждём не «active» (systemd ставит его сразу), а строку из лога самого бота:
# она означает, что токен принят и polling начался.
#
# Журнал читаем в переменную, а не через `journalctl | grep -q`: при pipefail
# grep -q закрывает пайп на первом же совпадении, journalctl получает SIGPIPE
# и возвращает 141 — и найденная строка читается как «не найдено».
for _ in $(seq "$HEALTH_TIMEOUT"); do
    JOURNAL="$(journalctl -u "$SERVICE" --since "$STARTED" --no-pager 2>/dev/null || true)"
    if [[ $JOURNAL == *"Бот запущен"* ]]; then
        HEALTHY=1
        break
    fi
    if ! systemctl is-active --quiet "$SERVICE"; then
        break   # упал совсем — дальше ждать нечего
    fi
    sleep 1
done

if [[ "${HEALTHY:-0}" != 1 ]]; then
    printf '\n✖ Бот не поднялся за %s с. Последние строки журнала:\n' "$HEALTH_TIMEOUT" >&2
    printf '%s\n' "${JOURNAL:-(журнал пуст)}" | tail -30 >&2
    rollback
    fail "деплой отменён"
fi

step "Готово"
sudo -u "$OWNER" "$PY" "$APP/migrate.py" --check | sed 's/^/  /'
echo "  версия кода: ${DEPLOY_SHA:-неизвестно}"
printf '%s\n' "$JOURNAL" | tail -5 | sed 's/^/  /'
