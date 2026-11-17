# alertmanager-telegram-relay

Маленький сервис без зависимостей: принимает webhook от Alertmanager и шлёт аккуратное сообщение в Telegram. Группирует алерты, ставит иконку по `severity` и экранирует HTML. Если группа не влезает в лимит Telegram (4096 символов), она делится на несколько сообщений `part 1/3`, `part 2/3`…, а не обрезается: при массовой аварии важен каждый алерт.

```
🚨 FIRING: HighErrorRate (2) · 🔕 silence
🟠 more than 5% of requests fail  api-7d9f · for 14m · runbook · graph
🟠 more than 5% of requests fail  api-x2kq · for 3m · runbook · graph
```

У каждого алерта видно, сколько он уже горит (а в RESOLVED, сколько длился), и ссылки на `runbook_url` и график в Prometheus. «🔕 silence» открывает форму тишины в Alertmanager с уже заполненными лейблами группы: дежурный глушит шумный алерт в два тапа с телефона. Ссылки берутся только с `http(s)://`, всё остальное из лейблов отбрасывается.

## Запуск

```bash
docker run -e TELEGRAM_TOKEN=... -e TELEGRAM_CHAT_ID=... -p 9087:9087 ghcr.io/sakuratix/alertmanager-telegram-relay
```

Чтобы писать в отдельную тему группы с топиками (forum), добавь `TELEGRAM_THREAD_ID`.

В Kubernetes: `deploy/k8s.yaml` (токен берётся из Secret, контейнер без root и с read-only FS).

## Alertmanager

```yaml
receivers:
  - name: telegram
    webhook_configs:
      - url: http://alertmanager-telegram-relay.monitoring:9087/alert
        send_resolved: true
```

Чтобы вебхук не мог дёрнуть кто угодно, кто достучался до порта, задай `WEBHOOK_TOKEN` (или `WEBHOOK_TOKEN_FILE`) и тот же токен в Alertmanager:

```yaml
    webhook_configs:
      - url: http://alertmanager-telegram-relay.monitoring:9087/alert
        http_config:
          authorization: { credentials_file: /etc/alertmanager/secrets/tg-relay/webhook_token }
```

Запросы без токена получают 401 и считаются в `relay_rejected_requests_total{reason="unauthorized"}`.

Для кнопки silence у Alertmanager должен быть задан `--web.external-url` (он приходит в вебхуке как `externalURL`).

Ошибки доставки:

- **429 Too Many Requests** (Telegram ограничивает частоту сообщений в чат): ждём столько, сколько он сказал в `retry_after`, и шлём снова;
- **5xx и сетевые ошибки**: повтор с экспоненциальной задержкой 0.5 → 1 → 2 с;
- **400/401/403** (неверный токен или chat id): не повторяем, это не пройдёт само;
- всё укладывается в 8 секунд. Если не получилось, сервис отвечает 502, и Alertmanager повторит отправку по своему расписанию, а не повиснет на таймауте.

## Метрики

`GET /metrics` в формате Prometheus: сколько вебхуков и алертов пришло, сколько сообщений доставлено, сколько доставок провалилось и когда была последняя успешная. В `deploy/k8s.yaml` есть ServiceMonitor и правило `TelegramRelayFailing`: сломанный канал алертов — ровно та поломка, о которой иначе никто не узнает.

## Почему не встроенный `telegram_configs`

Он есть в Alertmanager с v0.24, но шаблоны сообщений приходится писать на Go templates в конфиге. Здесь формат лежит в коде и покрыт тестами.

## Разработка

```bash
pytest -q
ruff check .
```
