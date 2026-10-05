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

В Telegram доступны четыре кнопки нижнего меню: «Видео», «Музыка», «История», «Поддержать». Если написать название песни, бот предлагает Spotify, SoundCloud или YouTube (музыка), показывает до 20 результатов по пять на странице и кнопки листания. Прямые ссылки на SoundCloud можно отправлять как обычно. Выбор видео 1080p/720p/360p и MP3 и пакетная отправка до 10 ссылок также сохранены.

Для **настоящего поиска в каталоге Spotify** создайте приложение в [Spotify for Developers](https://developer.spotify.com/dashboard) и задайте в Render секреты `SPOTIFY_CLIENT_ID` и `SPOTIFY_CLIENT_SECRET`. Без них кнопка Spotify сообщит, что источник не настроен. Для нового приложения в Spotify Development Mode владелец должен иметь Premium, действуют ограничения режима разработки; перед публичным запуском проверьте возможность Extended Quota Mode. Spotify предоставляет метаданные треков; MP3 бот ищет в доступных источниках SoundCloud или YouTube. Совпадение записи не гарантировано.

Для кнопки «Поддержать» задайте `PAYMENT_SUPPORT_USERNAME` — свой Telegram username без `@`. После этого бот предложит добровольные донаты 50, 100 и 250 Telegram Stars. Донат не даёт платных функций. По вопросам платежей пользователь может использовать `/paysupport`. Не помещайте Spotify secret или токен бота в Git.

История и состояние поиска хранятся в памяти и сбрасываются при рестарте Render. Файлы ограничены 49 МБ и удаляются после отправки. Платные лимиты и подписки нельзя надёжно включить без постоянной базы данных: на бесплатном Render локальные файлы теряются при перезапусках.

### Предложение по лимитам после подключения постоянной базы

- Бесплатно: 3 успешные загрузки в сутки; поиск до 20 раз в сутки; видео до 720p; пакетная загрузка до 3 ссылок.
- Пакет на 30 дополнительных загрузок: 150 Stars; видео до 1080p и пакетная загрузка до 10 ссылок. Кредиты списываются только после успешной отправки файла и не сгорают по времени.
- Донат 50/100/250 Stars остаётся добровольным и не меняет лимиты.

Это проект тарифов, а не действующее ограничение. Для платных кредитов нужны постоянная база данных, журнал платежей с защитой от повторной обработки и обработка возвратов. Перед включением цен проверьте фактические расходы на исходящий трафик Render и качество загрузки разных источников.
