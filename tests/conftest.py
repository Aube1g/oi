"""
Shared pytest fixtures for the XLI test suite.

`fake_provider` gives tests an AbstractProvider that needs no network and no
API key, so the suite can run in CI without secrets. Anything that would
otherwise call xli.providers.base.get_provider() should accept an injected
provider (see XliAgent in xli/core/chain.py) or monkeypatch get_provider.
"""

import pytest

from xli.providers.fake import FakeProvider
from xli.providers import base as provider_base
from xli.ui import locale


@pytest.fixture
def fake_provider():
    return FakeProvider(responses=["OK"])


@pytest.fixture(autouse=True)
def _reset_provider_singleton():
    """Prevent one test's cached provider from leaking into the next."""
    provider_base.reset_provider()
    yield
    provider_base.reset_provider()


@pytest.fixture(autouse=True)
def _pin_english_locale():
    """Tests assert English words ("ok", "FAIL", "working"); the UI default is
    Russian, so pin the language per test and unpin it afterwards."""
    locale.set_lang("en")
    yield
    locale.set_lang(None)
