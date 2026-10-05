# Telegram Media Downloader

Бот принимает ссылки на общедоступные материалы из TikTok, YouTube, Instagram,
Facebook, X/Twitter, Vimeo и SoundCloud, предлагает видео или MP3, поддерживает
пакетную загрузку и поиск музыки. Используется long polling и HTTP `/health`
для Render Web Service.

## Сначала замените раскрытый токен

Токен со скриншота считается скомпрометированным. В `@BotFather` выберите
`/revoke`, укажите `@mediadownloaderrrr_bot`, затем получите новый токен. Не
отправляйте его в чат и не добавляйте в Git.

## Быстрый локальный запуск

Требуется Python 3.10 или новее и `ffmpeg` (для некоторых сайтов):

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN='НОВЫЙ_ТОКЕН'
python bot.py
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:BOT_TOKEN = 'НОВЫЙ_ТОКЕН'
python bot.py
```

Токен намеренно не читается из `.env`: в production он хранится в отдельном
файле `/etc/telegram-media-bot.env`, доступном только root.

## Установка на Ubuntu/Debian VPS

Подключитесь к серверу и установите зависимости:

```bash
ssh root@IP_СЕРВЕРА
apt update
apt install -y python3 python3-venv ffmpeg rsync
useradd --system --home /var/lib/telegram-media-bot --create-home \
  --shell /usr/sbin/nologin telegrambot
mkdir -p /opt/telegram-media-bot
```

С локального компьютера загрузите содержимое этой папки:

```bash
rsync -av --exclude .venv --exclude __pycache__ ./telegram-bot/ \
  root@IP_СЕРВЕРА:/opt/telegram-media-bot/
```

На сервере создайте окружение и установите пакеты:

```bash
cd /opt/telegram-media-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
chown -R telegrambot:telegrambot /opt/telegram-media-bot
```

Создайте защищённый файл с **новым** токеном:

```bash
install -m 600 -o root -g root /dev/null /etc/telegram-media-bot.env
nano /etc/telegram-media-bot.env
```

Содержимое файла:

```ini
BOT_TOKEN=ВСТАВЬТЕ_НОВЫЙ_ТОКЕН
MAX_UPLOAD_MB=49
MAX_CONCURRENT_DOWNLOADS=2
```

Установите и запустите службу:

```bash
cp deploy/telegram-media-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telegram-media-bot
systemctl status telegram-media-bot --no-pager
```

Логи и перезапуск:

```bash
journalctl -u telegram-media-bot -f
systemctl restart telegram-media-bot
```

После изменения кода снова выполните `rsync`, затем
`systemctl restart telegram-media-bot`.

## Ограничения и защита

- Telegram Bot API принимает от обычного облачного бота файлы до 50 МБ; бот
  использует запас и ограничивает результат 49 МБ.
- Плейлисты отключены, на пользователя одновременно обрабатывается одна ссылка.
- Общая параллельность задаётся `MAX_CONCURRENT_DOWNLOADS` (по умолчанию 2).
- Временная папка удаляется после каждой отправки, включая ошибки.
- Разрешены только известные видеосервисы; произвольные URL не загружаются.
- Закрытые или возрастные материалы без cookies могут не скачиваться.
- Соблюдайте авторские права и правила сайтов-источников.

Проверка тестов:

```bash
python -m unittest discover -s tests -v
```

## Развёртывание на Render Web Service

1. Загрузите **содержимое папки `telegram-bot`** в отдельный приватный репозиторий GitHub. Не загружайте всю родительскую папку: там есть большие APK и другие проекты.
2. В Render выберите **New → Blueprint**, подключите репозиторий. `render.yaml` создаст бесплатный Web Service с Docker и проверкой `/health`.
3. В секретную переменную `BOT_TOKEN` внесите **новый** токен после `/revoke` в BotFather. Не публикуйте его в Git и не отправляйте в чат.
4. Дождитесь успешной сборки. В логах появится `Бот запущен в режиме long polling; HTTP /health`.
5. Откройте `https://ИМЯ-СЕРВИСА.onrender.com/health`: ответ должен быть `ok`.
6. Если нужен внешний монитор, настройте в нём GET этого URL каждые 5 минут. Монитор и его надёжность — отдельный сервис. Само наличие `/health` не отменяет правил бесплатного тарифа Render.

Бот использует long polling. При засыпании Web Service входящее сообщение Telegram само по себе не разбудит его: для этого нужен внешний HTTP-запрос к `/health`. Бесплатный Render может усыплять сервис после 15 минут без входящих HTTP-запросов, а также ограничивать месячные часы и перезапускать его. Поэтому непрерывную работу на бесплатном плане гарантировать нельзя.

В Telegram доступны четыре кнопки нижнего меню: «Видео», «Музыка», «История», «Plus+». Если написать название песни, бот предлагает Spotify, SoundCloud или YouTube (музыка), показывает до 20 результатов по пять на странице и кнопки листания. Прямые ссылки на SoundCloud можно отправлять как обычно. Выбор видео 1080p/720p/360p и MP3 и пакетная отправка до 10 ссылок также сохранены.

Для **настоящего поиска в каталоге Spotify** создайте приложение в [Spotify for Developers](https://developer.spotify.com/dashboard) и задайте в Render секреты `SPOTIFY_CLIENT_ID` и `SPOTIFY_CLIENT_SECRET`. Без них кнопка Spotify сообщит, что источник не настроен. Для нового приложения в Spotify Development Mode владелец должен иметь Premium, действуют ограничения режима разработки; перед публичным запуском проверьте возможность Extended Quota Mode. Spotify предоставляет метаданные треков; MP3 бот ищет в доступных источниках SoundCloud или YouTube. Совпадение записи не гарантировано.

Для платежей задайте `PAYMENT_SUPPORT_USERNAME` — свой Telegram username без `@`. После этого доступны Plus+ за 99 Telegram Stars и добровольные донаты 50, 100 и 250 Stars. Донат не даёт платных функций. По вопросам платежей пользователь может использовать `/paysupport`. Не помещайте Spotify secret, строку базы или токен бота в Git и чат.

История ссылок и состояние поиска хранятся в памяти и сбрасываются при рестарте Render. Дневные лимиты и платежи хранятся в PostgreSQL. Файлы удаляются после отправки.

### Лимиты и Plus+

- Бесплатно: 5 успешных видео или аудиофайлов за 24 часа с первого успешного файла; до 25 МБ и 720p; пакетная загрузка до 3 ссылок.
- Plus+ за 99 Stars: 30 успешных файлов за 24 часа с покупки; до 49 МБ, до 1080p и пакетная загрузка до 10 ссылок. Каждый файл в пакете считается отдельно. Неудачная загрузка не списывается.
- Донат 50/100/250 Stars остаётся добровольным и не меняет лимиты.

Покупка Plus+ включается только если настроены `DATABASE_URL` и `PAYMENT_SUPPORT_USERNAME`. Повторная обработка одного платежа не добавляет новый доступ; при возврате Stars соответствующий доступ отменяется. Без базы бот продолжает работать в прежнем режиме без лимитов, но не принимает оплату за Plus+.

### Подключение базы на Render

1. Создайте Render Postgres (для временной проверки допустим Free), если его ещё нет.
2. В Web Service → Environment добавьте `DATABASE_URL` со строкой подключения к базе (предпочтительно Internal Database URL). Render передаёт её как секрет. Не добавляйте адрес с паролем в Git, логи или чат.
3. Добавьте `PAYMENT_SUPPORT_USERNAME` и сохраните переменные. После нового деплоя в логах не должно быть ошибок подключения к базе; таблицы создаются автоматически.
4. Проверьте `/start`, пять загрузок, сообщение об окончании лимита и тестовую оплату Stars. Для проверки платежей используйте тестовое окружение Telegram, прежде чем принимать реальные Stars.

Бесплатный Render Postgres истекает через 30 дней. До истечения срока перенесите данные в постоянную базу или обновите тариф базы, иначе платные доступы и учёт лимитов станут недоступны.
