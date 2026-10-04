# Плагин XLI для Claude Code

Плагин из двух частей:

* `commands/` — слэш-команды `/xli`, `/xli-graph`, `/xli-tools`;
* `.mcp.json` — MCP-сервер `xli`, который отдаёт этому сеансу инструменты
  XLI: чтение и правку файлов, поиск, `bash`, граф зависимостей (`dep_graph`),
  карту репозитория (`repo_map`), графики в терминале (`chart`), суб-агентов
  (`delegate`) и остальное из `xli tools list`.

## Установка

```bash
xli claude install --plugin-dir ~/.claude/plugins/xli
```

или вручную: положите каталог в проект и добавьте его как marketplace

```
/plugin marketplace add ./xli/claude/plugin_root
```

Проверка: `/mcp` в Claude Code должен показать сервер `xli` и 23 инструмента;
`xli mcp list` покажет то же самое с другой стороны.
