"""The web frontend: the assets, and the JavaScript actually running.

`app.js` is loaded into Node's own `vm` with a small DOM stub, and its
renderer is called the way the page calls it. That catches the things a grep
cannot: markdown that stops stripping its markers, a list that never closes,
Russian plurals that say «3 вызовов», and the `undefined` that appears in a
chat window when a field is spelled wrong.

Node is optional (`node` is not required to run XLI); these tests skip when it
is missing, and the static checks below always run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "xli" / "web"

NODE = shutil.which("node")

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


class TestAssets:
    def test_the_three_files_are_there(self):
        for name in ("index.html", "style.css", "app.js"):
            assert (WEB / name).is_file(), f"нет {name}"

    def test_the_page_is_russian_and_utf8(self):
        html = read("index.html")
        assert '<meta charset="utf-8">' in html
        assert re.search(r'<html[^>]*lang="ru"', html), "страница не объявлена русской"
        assert CYRILLIC.search(html)

    def test_the_page_carries_no_emoji(self):
        for name in ("index.html", "style.css", "app.js"):
            for line_no, line in enumerate(read(name).splitlines(), start=1):
                bad = [char for char in line if ord(char) > 0x2BFF]
                assert not bad, f"{name}:{line_no}: {bad}"

    def test_nothing_is_loaded_from_the_network(self):
        """No CDN, no fonts, no analytics: the page must work offline, since
        the kernel it talks to is a local process."""
        html = read("index.html")
        for match in re.findall(r'(?:src|href)="([^"]+)"', html):
            assert not match.startswith(("http://", "https://", "//")), match
        assert "fonts.googleapis" not in html and "cdn" not in html

    def test_the_client_speaks_to_the_same_kernel(self):
        js = read("app.js")
        assert 'fetch("/rpc"' in js
        assert 'new EventSource("/events")' in js

    def test_the_kernel_methods_it_calls_exist(self):
        from xli.kernel.methods import build_kernel

        methods = {spec["name"] for spec in build_kernel().list_methods()}
        called = set(re.findall(r'rpc\("([\w.]+)"', read("app.js")))
        assert called, "страница ни о чём не спрашивает ядро"
        missing = sorted(called - methods)
        assert not missing, f"ядро не знает методов: {missing}"

    def test_the_palette_is_violet(self):
        css = read("style.css")
        assert "--accent: #a679ff" in css
        assert css.count("#a679ff") + css.count("--accent") > 3

    def test_animation_is_opt_out(self):
        assert "prefers-reduced-motion" in read("style.css")

    def test_the_frontend_ships_in_the_wheel(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert '"xli.web"' in pyproject


@pytest.fixture(scope="module")
def rendered() -> dict:
    """app.js executed in Node, with the DOM stubbed out."""
    if NODE is None:
        pytest.skip("нужен node, чтобы выполнить app.js")
    harness = ROOT / "tests" / "fixtures" / "web_probe.js"
    harness.write_text(TestJavaScript.HARNESS, encoding="utf-8")
    try:
        result = subprocess.run(
            [NODE, str(harness), str(WEB / "app.js")],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        harness.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="нужен node, чтобы выполнить app.js")
class TestJavaScript:
    """Run app.js for real, with a DOM stub around it."""

    HARNESS = """
    const fs = require("fs");
    const vm = require("vm");

    const listeners = {};
    // One stable node per id, so a test can read back what the page wrote —
    // the roster of sub-agents lives in #st-agents, for instance.
    const nodes = {};
    function makeNode(id) {
      return {
        id, classList: { add: () => {}, remove: () => {}, contains: () => false },
        style: {}, dataset: {}, children: [], hidden: false,
        appendChild(child) { this.children.push(child); return child; },
        querySelector: () => null, querySelectorAll: () => [],
        addEventListener: () => {}, remove: () => {},
        textContent: "", innerHTML: "", className: "",
      };
    }
    const context = {
      console,
      performance: { now: () => Date.now() },
      setTimeout: () => 0,
      clearTimeout: () => {},
      fetch: () => Promise.reject(new Error("no network in tests")),
      EventSource: function () {},
      localStorage: { getItem: () => null, setItem: () => {} },
      window: {
        setTimeout: () => 0, clearTimeout: () => {},
        requestAnimationFrame: (fn) => fn(),
        addEventListener: () => {}, scrollTo: () => {},
        innerHeight: 800, scrollY: 0,
        document: null,
        localStorage: { getItem: () => null, setItem: () => {} },
      },
      document: {
        documentElement: { dataset: { theme: "dark" } },
        body: { scrollHeight: 1200, scrollTop: 0, appendChild: () => {} },
        addEventListener: (name, fn) => { listeners[name] = fn; },
        querySelector: () => null,
        querySelectorAll: () => [],
        getElementById: (id) => (nodes[id] = nodes[id] || makeNode(id)),
        createElement: (tag) => makeNode(tag),
      },
    };
    context.globalThis = context;
    context.window.document = context.document;

    const code = fs.readFileSync(process.argv[2], "utf8") +
      "\\nglobalThis.__xli = { renderMarkdown, plural, duration, glyph, argSummary,"
      + " inlineSpans, agentColor, agentOf, addDelegate, handleNotification,"
      + " APP_VERSION_CHECK: 1 };";
    vm.createContext(context);
    vm.runInContext(code, context);

    const out = {
      markdown: {},
      plural: {},
      duration: {},
      glyph: {},
      args: {},
    };

    const cases = {
      heading: "# Заголовок",
      nested: "- один\\n  - два",
      ordered: "1. первый\\n2. второй",
      fence: "```python\\nprint(1)\\n```",
      table: "| a | b |\\n| --- | --- |\\n| 1 | 2 |",
      link: "смотри [документацию](https://example.com/x)",
      mark: "это ==важное== слово",
      bold: "**жирно** и *курсив*",
      quote: "> цитата",
      rule: "---",
      tasks: "- [x] готово\\n- [ ] нет",
    };
    for (const [name, text] of Object.entries(cases)) {
      out.markdown[name] = context.__xli.renderMarkdown(text);
    }
    for (const n of [1, 2, 3, 5, 11, 21, 22, 25]) {
      out.plural[n] = context.__xli.plural(n, "вызов", "вызова", "вызовов");
    }
    out.duration.short = context.__xli.duration(3.44);
    out.duration.long = context.__xli.duration(125);
    out.glyph.known = context.__xli.glyph("chart");
    out.glyph.unknown = context.__xli.glyph("нечтознакомое");
    out.args.path = context.__xli.argSummary({ path: "a.py", other: "x" });
    out.args.fallback = context.__xli.argSummary({ query: "поиск" });
    out.args.empty = context.__xli.argSummary({});

    // Sub-agents: a delegate call, a child coming and going, and the roster.
    out.colours = {
      reviewer: context.__xli.agentColor("reviewer"),
      explorer: context.__xli.agentColor("explorer"),
      debugger: context.__xli.agentColor("debugger"),
      testwriter: context.__xli.agentColor("test-writer"),
      documenter: context.__xli.agentColor("documenter"),
      hinted: context.__xli.agentColor("reviewer", "good"),
      stable: context.__xli.agentColor("reviewer") === context.__xli.agentColor("reviewer"),
    };
    out.agents = {
      main: context.__xli.agentOf({ agent_id: "main", agent_name: "xli" }),
      child: context.__xli.agentOf({ agent_id: "reviewer-1a2b", agent_name: "reviewer" }),
    };
    const delegate = context.__xli.addDelegate("reviewer", "посмотри парсер");
    out.delegate_html = delegate.innerHTML;
    context.__xli.handleNotification("agent.tool_call",
      { name: "delegate", args: { agent_name: "explorer", task: "найди вход", path: "xli/cli.py" } });
    context.__xli.handleNotification("agent.agent",
      { phase: "start", agent_id: "explorer-1", agent_name: "explorer", task: "найди вход" });
    context.__xli.handleNotification("agent.agent",
      { phase: "end", agent_id: "explorer-1", agent_name: "explorer", steps: 3,
        seconds: 1.5, stopped_reason: "done" });
    out.roster = nodes["st-agents"] ? nodes["st-agents"].textContent : null;
    out.transcript = (nodes["transcript"] ? nodes["transcript"].children : []).map(
      (child) => child.className + "|" + String(child.textContent || child.innerHTML || "")
    );

    process.stdout.write(JSON.stringify(out));
    """


    def test_headings_lose_their_hashes(self, rendered):
        assert rendered["markdown"]["heading"] == "<h1>Заголовок</h1>"

    def test_nested_lists_step_in(self, rendered):
        html = rendered["markdown"]["nested"]
        assert html.count("<ul>") == 1 and html.endswith("</ul>")
        assert "<li>один</li>" in html, "верхний пункт без отступа"
        assert 'margin-left:1.2em' in html, "вложенный пункт не сдвинут"

    def test_ordered_lists_stay_ordered(self, rendered):
        assert rendered["markdown"]["ordered"].startswith("<ol>")
        assert rendered["markdown"]["ordered"].endswith("</ol>")

    def test_code_fences_become_pre_blocks(self, rendered):
        html = rendered["markdown"]["fence"]
        assert html.startswith('<pre data-lang="python"><code>')
        assert html.endswith("</code></pre>")

    def test_tables_are_closed(self, rendered):
        html = rendered["markdown"]["table"]
        assert html.count("<table>") == 1
        assert html.endswith("</table>")
        assert "<th>a</th>" in html

    def test_a_link_keeps_only_its_label(self, rendered):
        assert rendered["markdown"]["link"] == "<p>смотри документацию</p>"

    def test_markers_are_stripped(self, rendered):
        assert rendered["markdown"]["mark"] == "<p>это <mark>важное</mark> слово</p>"
        bold = rendered["markdown"]["bold"]
        assert "<strong>жирно</strong>" in bold and "<em>курсив</em>" in bold
        assert "**" not in bold and "*" not in bold.replace("<em>", "").replace("</em>", "")

    def test_quotes_and_rules_render(self, rendered):
        assert rendered["markdown"]["quote"] == "<blockquote>цитата</blockquote>"
        assert rendered["markdown"]["rule"] == "<hr>"

    def test_task_lists_use_tick_boxes(self, rendered):
        html = rendered["markdown"]["tasks"]
        assert "☑ готово" in html and "☐ нет" in html

    def test_nothing_renders_as_undefined(self, rendered):
        for name, html in rendered["markdown"].items():
            assert "undefined" not in html, name
            assert "[object Object]" not in html, name

    def test_russian_plurals(self, rendered):
        assert rendered["plural"]["1"] == "вызов"
        assert rendered["plural"]["2"] == "вызова"
        assert rendered["plural"]["5"] == "вызовов"
        assert rendered["plural"]["11"] == "вызовов"
        assert rendered["plural"]["21"] == "вызов"
        assert rendered["plural"]["22"] == "вызова"

    def test_durations_are_russian(self, rendered):
        assert rendered["duration"]["short"] == "3,4 с"
        assert rendered["duration"]["long"] == "2 мин 05 с"

    def test_tool_glyphs_have_a_fallback(self, rendered):
        assert rendered["glyph"]["known"] == "▁"
        assert rendered["glyph"]["unknown"] == "◆"

    def test_argument_summaries_pick_the_identifying_key(self, rendered):
        assert rendered["args"]["path"] == "a.py"
        assert rendered["args"]["fallback"] == "поиск"
        assert rendered["args"]["empty"] == ""

    # ------------------------------------------------------------ sub-agents
    def test_every_sub_agent_gets_its_own_colour(self, rendered):
        colours = rendered["colours"]
        assert colours["stable"], "цвет суб-агента не должен меняться между запусками"
        seen = [colours["reviewer"], colours["explorer"], colours["debugger"]]
        assert len(set(seen)) == 3, f"суб-агенты сливаются в один цвет: {seen}"
        assert all(re.fullmatch(r"#[0-9A-F]{6}", c) for c in seen)

    def test_the_browser_and_the_terminal_agree_on_the_colour(self, rendered):
        """One mapping, two front ends: the hash must be the same one."""
        from xli.ui.agents import agent_color

        for name in ("reviewer", "explorer", "debugger", "test-writer", "documenter"):
            key = "testwriter" if name == "test-writer" else name
            assert rendered["colours"][key] == agent_color(name), name

    def test_a_spec_colour_beats_the_hash(self, rendered):
        """`test-writer` asks for `colour: good`; the hint must win."""
        from xli.ui.agents import agent_color

        assert rendered["colours"]["hinted"] == agent_color("reviewer", "good")

    def test_the_main_agent_is_not_painted_as_a_delegate(self, rendered):
        assert rendered["agents"]["main"]["id"] == "main"
        assert rendered["agents"]["main"]["colour"] == ""
        child = rendered["agents"]["child"]
        assert child["id"] == "reviewer-1a2b" and child["name"] == "reviewer"
        assert child["colour"] == rendered["colours"]["reviewer"]

    def test_a_delegate_call_announces_itself_in_the_target_colour(self, rendered):
        html = rendered["delegate_html"]
        assert "делегирую" in html
        assert "reviewer" in html
        assert rendered["colours"]["reviewer"] in html, "имя суб-агента не в его цвете"

    def test_the_roster_shows_who_is_working(self, rendered):
        assert rendered["roster"] == "explorer", "панель состояния не назвала суб-агента"

    def test_a_child_arriving_and_leaving_is_visible(self, rendered):
        rows = [row for row in rendered["transcript"] if "explorer" in row]
        assert any("подключился" in row for row in rows), rows
        assert any("закончил" in row for row in rows), rows
        assert any("шага" in row and "1,5 с" in row for row in rows), rows
