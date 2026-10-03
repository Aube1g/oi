#!/usr/bin/env python3
"""
XLI Locale — the single table every front end renders its words from.

The CLI, the REPL and the TUI all ask `t()` for what to show, so a language
switch is one config key (`ui.lang`), not a scavenger hunt through print
calls. Russian is the default: the project is written for a Russian speaker
first, and English stays one key away.

Resolution order for the language: explicit `set_lang()` (the CLI passes the
config value), then `XLI_LANG`, then the process locale, then Russian.

Provider errors get special treatment: the raw exception text is a wall of
JSON from somebody else's gateway, and `humanise_provider_error` turns the
common shapes (timeout, no credits, bad key, rate limit, no network) into one
readable line in the current language.
"""

from __future__ import annotations

import os
import re

_EN: dict[str, str] = {
    "ok": "ok",
    "fail": "FAIL",
    "repaired": "  repaired: {detail}",
    "warning": "  warning: {message}",
    "error": "  error: {message}",
    "step": "-- step {index}/{max_steps}",
    "needs_approval": "  {tool} needs approval ({reason})",
    "args": "    args: {args}",
    "allow_prompt": "  allow? [y/N] ",
    "stop_done": "done",
    "stop_no_tool_calls": "answer without tool calls",
    "stop_provider_error": "provider error",
    "stop_max_steps": "step limit reached",
    "session": "session {session_id}",
    "environment": "environment: {exc}",
    "env_hint": "  set the API key, or run `xli config set provider <name>`",
    "err_timeout": "the provider stayed silent for {seconds}s — no answer, just try again",
    "err_balance": "no spendable credits left on the provider — the allowance refills soon",
    "err_auth": "the provider rejected the API key — check it in `xli config`",
    "err_rate": "the provider is rate-limiting — wait a little and retry",
    "err_connect": "could not reach the provider — check the network",
    "err_provider": "provider error: {detail}",
    "tui_panel_plan": "plan",
    "tui_panel_graph": "graph",
    "tui_panel_agents": "agents",
    "tui_panel_stats": "stats",
    "tui_panel_hint": "tab — next pane · 1-4 — pick · esc — close",
    "tui_plan_empty": "no tasks yet",
    "tui_plan_progress": "{done}/{total} done",
    "tui_graph_hint": "xli graph <module> for details",
    "tui_agents_empty": "no delegates yet",
    "tui_activity_thinking": "thinking",
    "tui_activity_tool": "{tool}…",
    "tui_activity_waiting": "waiting for approval",
    "tui_activity_writing": "writing the answer",
    "tui_activity_delegating": "delegating to {agent}",
    "tui_scroll": "line {pos}/{total}",
    "unit_ms": "{n} ms",
    "unit_s": "{n} s",
    "steps_word": "{n} step(s)",
    "tui_agent_done": "done · {steps} · {seconds}",
    "graph_modules": "{n} modules",
    "graph_edges": "{n} links",
    "graph_cycles": "import cycles: {n}",
    "graph_none": "none",
    "graph_hot": "the project leans on these",
    "graph_entries": "entry points (nothing imports them)",
    "impact_direct": "imported directly by ({n})",
    "impact_chain": "affected through the chain ({n})",
    "impact_deps": "depends on ({n})",
    "impact_tests": "tests that mention it ({n})",
    "impact_free": "nothing imports it — safe to change",
    "not_found": "not found: {what}",
    "panel_plan_progress": "{done}/{total} done",
    "tui_working": "working",
    "tui_idle": "idle",
    "tui_quit": "^C quit",
    "tui_steps": "steps {n}",
    "tui_tools": "tools {n}",
    "tui_errors": "errors {n}",
    "tui_approve": " approve {tool}? ",
    "tui_approve_keys": " y allow · n refuse · a allow all ",
    "tui_approved": "approved",
    "tui_refused": "refused",
    "tui_already": "already working — wait for it to finish",
    "tui_unknown_cmd": "unknown command: /{command} — try /help",
    "tui_mode": "permission mode: {mode}",
    "tui_model": "model: {model}",
    "tui_session": "session: {session}",
    "tui_tools_list": "tools: {names}",
    "tui_hint": "a task for the agent… (/help — keys)",
    "tui_you": "you ",
    "tui_ready": "ready ",
    "tui_keys": "^C quit · ^L redraw · Enter send · ↑ history",
    "tui_step": "step {index}/{max_steps}",
    "tui_tokens": "tokens {n}",
    "tui_history_note": "— history above · new messages below —",
    "tui_small": "terminal too small ({width}x{height}) — need at least 20x8",
    "skills_count": "{n} skill(s)",
    "skills_none": "no skills defined",
    "skills_found": "{n} skill(s) matching '{query}'",
    "skills_none_found": "nothing found for '{query}'",
    "skills_search_usage": "usage: xli skills search WORDS…",
    "kernel_methods": "{n} kernel method(s)",
    "guides_count": "{n} guide(s)",
    "guides_read_hint": "read one with: xli guides read NAME",
    "guides_unknown": "unknown guide: {name} — see `xli guides`",
    "repl_help_note": "type a task, or /help for commands",
    "repl_allow_prompt": "  allow? [y/N/a=always] ",
    "repl_mutates": "mutates",
    "repl_read": "read",
    "repl_mode_usage": "usage: /mode auto|confirm|readonly",
    "repl_deny_usage": "usage: /deny PATTERN",
    "repl_model_usage": "usage: /model NAME",
    "repl_interrupted": "  interrupted",
}

_RU: dict[str, str] = {
    "ok": "ок",
    "fail": "ОШИБКА",
    "repaired": "  починено: {detail}",
    "warning": "  внимание: {message}",
    "error": "  ошибка: {message}",
    "step": "-- шаг {index}/{max_steps}",
    "needs_approval": "  {tool}: нужно подтверждение ({reason})",
    "args": "    аргументы: {args}",
    "allow_prompt": "  разрешить? [y/N] ",
    "stop_done": "готово",
    "stop_no_tool_calls": "ответ без вызовов инструментов",
    "stop_provider_error": "ошибка провайдера",
    "stop_max_steps": "достигнут лимит шагов",
    "session": "сессия {session_id}",
    "environment": "окружение: {exc}",
    "env_hint": "  задайте API-ключ или выполните `xli config set provider <name>`",
    "err_timeout": "провайдер молчал {seconds} с — ответа нет, просто попробуйте ещё раз",
    "err_balance": "у провайдера кончились кредиты — лимит скоро обновится",
    "err_auth": "провайдер не принял API-ключ — проверьте его в `xli config`",
    "err_rate": "провайдер ограничил запросы — подождите немного и повторите",
    "err_connect": "не удалось достучаться до провайдера — проверьте сеть",
    "err_provider": "ошибка провайдера: {detail}",
    "tui_panel_plan": "план",
    "tui_panel_graph": "граф",
    "tui_panel_agents": "агенты",
    "tui_panel_stats": "статистика",
    "tui_panel_hint": "tab — следующая панель · 1-4 — выбрать · esc — закрыть",
    "tui_plan_empty": "задач пока нет",
    "tui_plan_progress": "{done}/{total} готово",
    "tui_graph_hint": "подробности: xli graph ИМЯ",
    "tui_agents_empty": "делегатов пока нет",
    "tui_activity_thinking": "думаю",
    "tui_activity_tool": "{tool}…",
    "tui_activity_waiting": "жду подтверждения",
    "tui_activity_writing": "пишу ответ",
    "tui_activity_delegating": "делегирую {agent}",
    "tui_scroll": "строка {pos}/{total}",
    "unit_ms": "{n} мс",
    "unit_s": "{n} с",
    "steps_word": "{n} шаг(ов)",
    "tui_agent_done": "готово · {steps} · {seconds}",
    "graph_modules": "{n} модулей",
    "graph_edges": "{n} связей",
    "graph_cycles": "циклы импорта: {n}",
    "graph_none": "нет",
    "graph_hot": "на них держится проект",
    "graph_entries": "точки входа (никто не импортирует)",
    "impact_direct": "импортируют напрямую ({n})",
    "impact_chain": "заденет по цепочке ({n})",
    "impact_deps": "сам зависит от ({n})",
    "impact_tests": "тесты, которые его упоминают ({n})",
    "impact_free": "никто не импортирует — можно менять свободно",
    "not_found": "не найдено: {what}",
    "panel_plan_progress": "{done}/{total} готово",
    "tui_working": "работаю",
    "tui_idle": "ожидание",
    "tui_quit": "^C выход",
    "tui_steps": "шаги {n}",
    "tui_tools": "инстр. {n}",
    "tui_errors": "ошибки {n}",
    "tui_approve": " разрешить {tool}? ",
    "tui_approve_keys": " y да · n нет · a всё ",
    "tui_approved": "разрешено",
    "tui_refused": "отклонено",
    "tui_already": "уже работает — дождитесь конца",
    "tui_unknown_cmd": "неизвестная команда: /{command} — попробуйте /help",
    "tui_mode": "режим прав: {mode}",
    "tui_model": "модель: {model}",
    "tui_session": "сессия: {session}",
    "tui_tools_list": "инструменты: {names}",
    "tui_hint": "задача для агента… (/help — клавиши, Tab — панели)",
    "tui_you": "вы ",
    "tui_ready": "готов",
    "tui_keys": "^C выход · Tab панели · ↑ история",
    "tui_step": "шаг {index}/{max_steps}",
    "tui_tokens": "токены {n}",
    "tui_history_note": "— выше история · новые сообщения ниже —",
    "tui_small": "терминал слишком мал ({width}x{height}) — нужно 20x8",
    "skills_count": "скиллов: {n}",
    "skills_none": "скиллы не определены",
    "skills_found": "по запросу '{query}': {n} скилл(ов)",
    "skills_none_found": "по запросу '{query}' ничего не нашлось",
    "skills_search_usage": "использование: xli skills search СЛОВА…",
    "kernel_methods": "методов ядра: {n}",
    "guides_count": "гайдов: {n}",
    "guides_read_hint": "читать так: xli guides read ИМЯ",
    "guides_unknown": "нет такого гайда: {name} — смотрите `xli guides`",
    "repl_help_note": "введите задачу или /help — список команд",
    "repl_allow_prompt": "  разрешить? [y/N/a=всегда] ",
    "repl_mutates": "пишет",
    "repl_read": "читает",
    "repl_mode_usage": "использование: /mode auto|confirm|readonly",
    "repl_deny_usage": "использование: /deny ШАБЛОН",
    "repl_model_usage": "использование: /model ИМЯ",
    "repl_interrupted": "  прервано",
}

_TABLES = {"en": _EN, "ru": _RU}

#: Set by `configure()`; None means "detect on first use".
_current: str | None = None


def set_lang(lang: str | None) -> None:
    """Pin the language explicitly; None or unknown values fall back to detect."""
    global _current
    if lang in _TABLES:
        _current = lang
    elif lang in (None, ""):
        _current = None
    else:
        _current = None


def detect_lang() -> str:
    """XLI_LANG beats the process locale; without a hint, Russian wins."""
    explicit = os.environ.get("XLI_LANG", "").strip().lower()
    if explicit in _TABLES:
        return explicit
    for var in ("LC_ALL", "LANG"):
        value = os.environ.get(var, "").strip().lower()
        if value.startswith("ru"):
            return "ru"
        if value.startswith("en"):
            return "en"
    return "ru"


def lang() -> str:
    return _current or detect_lang()


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form for `n`, with an English-friendly fallback.

    "1 шаг", "2 шага", "5 шагов" — a counter that says "1 шагов" looks like a
    bug even when the number is right, and the interface is in Russian first.
    """
    number = abs(int(n))
    if lang() != "ru":
        return one if number == 1 else many
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def steps_word(n: int) -> str:
    """`3 шага` — the counter used by panels and delegate summaries."""
    return f"{n} {plural(n, 'шаг', 'шага', 'шагов')}"


def seconds_word(n: float) -> str:
    """`1,4 с` — Russian decimal comma, one decimal place."""
    return f"{n:.1f}".replace(".", ",") + " с"


def t(key: str, **kwargs: object) -> str:
    """The string for `key` in the current language, formatted.

    Missing keys degrade in stages: current language, English, then the key
    itself — a gap in a table must never crash a front end.
    """
    table = _TABLES.get(lang(), _EN)
    template = table.get(key) or _EN.get(key) or key
    try:
        return template.format(**kwargs) if kwargs else template
    except (KeyError, IndexError):
        return template


def configure(config: object | None) -> None:
    """Read `ui.lang` from a loaded Config (or anything with .get)."""
    value: str | None = None
    try:
        value = config.get("ui.lang") if config is not None else None  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - locale must never break startup
        value = None
    set_lang(value if isinstance(value, str) else None)


_TIMEOUT = re.compile(r"timed?\s*out\s*after\s*([\d.]+)\s*s", re.IGNORECASE)


def humanise_provider_error(message: str) -> str:
    """One readable line for the usual provider failures, in the current lang.

    The raw message is often a JSON dump from somebody else's gateway; the
    user needs the *reason*, not the payload.
    """
    text = message or ""
    match = _TIMEOUT.search(text)
    if match:
        return t("err_timeout", seconds=match.group(1))
    lowered = text.lower()
    if "402" in text or "insufficient" in lowered or "balance" in lowered or "credit" in lowered:
        return t("err_balance")
    if (
        "401" in text
        or "403" in text
        or "auth" in lowered
        or "api key" in lowered
        or "api_key" in lowered
    ):
        return t("err_auth")
    if "429" in text or "rate" in lowered:
        return t("err_rate")
    if "connect" in lowered or "unreachable" in lowered or "dns" in lowered:
        return t("err_connect")
    detail = text.strip()
    if len(detail) > 220:
        detail = detail[:217] + "..."
    return t("err_provider", detail=detail or "?")
