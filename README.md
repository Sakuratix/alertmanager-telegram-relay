# alertmanager-telegram-relay

Маленький сервис без зависимостей: принимает webhook от Alertmanager и шлёт аккуратное сообщение в Telegram. Группирует алерты, ставит иконку по `severity`, экранирует HTML и укладывается в лимит Telegram на длину сообщения.

```
🚨 FIRING: HighErrorRate (2)
🟠 more than 5% of requests fail  api-7d9f
🟠 more than 5% of requests fail  api-x2kq
```

## Запуск

```bash
docker run -e TELEGRAM_TOKEN=... -e TELEGRAM_CHAT_ID=... -p 9087:9087 ghcr.io/sakuratix/alertmanager-telegram-relay
```

В Kubernetes: `deploy/k8s.yaml` (токен берётся из Secret, контейнер без root и с read-only FS).

## Alertmanager

```yaml
receivers:
  - name: telegram
    webhook_configs:
      - url: http://alertmanager-telegram-relay.monitoring:9087/alert
        send_resolved: true
```

Если Telegram недоступен, сервис отвечает 502, и Alertmanager повторит отправку сам.

## Почему не встроенный `telegram_configs`

Он есть в Alertmanager с v0.24, но шаблоны сообщений приходится писать на Go templates в конфиге. Здесь формат лежит в коде и покрыт тестами.

## Разработка

```bash
pytest -q
ruff check .
```
