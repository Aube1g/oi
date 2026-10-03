from xli.core.sandbox import get_sandbox


def test_sandbox_constructs():
    # Regression: Config was missing get_sandbox_timeout/get_sandbox_max_memory/
    # is_network_disabled, so CodeSandbox() raised AttributeError on creation.
    sandbox = get_sandbox()
    assert sandbox is not None


def test_sandbox_is_singleton():
    # Regression: get_sandbox() used to return a fresh CodeSandbox() every call.
    assert get_sandbox() is get_sandbox()


def test_sandbox_runs_safe_code():
    result = get_sandbox().execute("print(1 + 1)")
    assert result["success"]
    assert result["output"].strip() == "2"


def test_sandbox_blocks_os_import():
    result = get_sandbox().execute("import os\nos.system('echo hi')")
    assert not result["success"]
