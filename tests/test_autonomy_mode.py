import os
import pytest

from xli.core.chain import AutonomyMode


@pytest.fixture(autouse=True)
def _clean_env():
    old = os.environ.pop("XLI_AUTONOMY_MODE", None)
    yield
    if old is not None:
        os.environ["XLI_AUTONOMY_MODE"] = old
    else:
        os.environ.pop("XLI_AUTONOMY_MODE", None)


def test_from_env_defaults_to_manual_when_unset():
    assert AutonomyMode.from_env() == AutonomyMode.MANUAL


def test_from_env_reads_valid_value_case_insensitively():
    os.environ["XLI_AUTONOMY_MODE"] = "AUTONOMOUS"
    assert AutonomyMode.from_env() == AutonomyMode.AUTONOMOUS


def test_from_env_falls_back_to_manual_on_invalid_value():
    os.environ["XLI_AUTONOMY_MODE"] = "bogus"
    assert AutonomyMode.from_env() == AutonomyMode.MANUAL


def test_validate_accepts_all_known_modes():
    for mode in (AutonomyMode.MANUAL, AutonomyMode.ASSISTED, AutonomyMode.AUTONOMOUS):
        assert AutonomyMode.validate(mode) == mode


def test_validate_rejects_unknown_mode():
    with pytest.raises(ValueError):
        AutonomyMode.validate("not-a-real-mode")
