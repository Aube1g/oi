# Ядро: JSON-RPC и Cython

## Что внутри

```bash
xli kernel info      # все RPC-методы с описаниями
xli kernel status    # что собрано, что в исходниках
```

Ядро — это JSON-RPC сервер (`xli serve`), через который TUI, Neovim-плагин и
любой внешний клиент общаются с xli одним протоколом.

## Сборка Cython

```bash
xli kernel preflight   # есть ли компилятор и cython
xli kernel build       # собрать xli.core/*
xli kernel clean
```

Без собранного ядра всё работает на чистом Python — сборка про скорость,
а не про возможности.

## Из Neovim

```bash
xli nvim install
```

Плагин поднимает `xli serve` и гоняет JSON-RPC по stdio: чат, diff-правки и
статусы берутся из тех же методов, что и `xli kernel info` показывает.
