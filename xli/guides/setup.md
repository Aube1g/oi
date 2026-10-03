# Настройка

## Где живёт конфиг

Ключи складываются по приоритету: значения по умолчанию → `~/.xli/config.json`
→ `./.xli/config.json` (проект) → переменные окружения `XLI__*`.

```bash
xli config list            # всё, что сейчас действует
xli config get provider    # одно значение
xli config set ui.lang ru  # язык интерфейса
```

## Провайдеры

- `openai` — любой OpenAI-совместимый шлюз (`provider.base_url`).
- `anthropic` — Claude и совместимые.
- `ollama`, `lmstudio` — локальные модели без ключа.

Ключ берётся из окружения (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …) или из
`~/.xli/.env`.

## Права

```bash
xli config set permissions.mode confirm
```

`confirm` — каждый пишущий вызов спрашивает; в TUI отвечайте `y`/`n`, `a` —
разрешить всё в этой сессии.

## Снапшоты

```bash
xli snapshot create     # перед рискованной правкой
xli snapshot list
xli snapshot restore <id>
```
