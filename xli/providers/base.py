#!/usr/bin/env python3
"""
XLI Providers Base — AbstractProvider + factory get_provider()
"""

from abc import ABC, abstractmethod
from typing import Any

from xli.manager.config import get_config
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.providers")


class AbstractProvider(ABC):
    """Base class for all LLM providers"""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key
        self.model = model
        self.logger = StructuredLogger(f"xli.providers.{self.__class__.__name__.lower()}")

    @abstractmethod
    async def chat(self, messages: list[dict], temperature: float = 0.4,
                   max_tokens: int = 4000) -> str:
        """Send chat completion request"""
        pass

    @abstractmethod
    async def stream(self, messages: list[dict], temperature: float = 0.4) -> Any:
        """Stream chat completion"""
        pass

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Get embeddings"""
        pass


#: Providers that run locally and need no credentials. `fake` is the offline
#: one: it answers from a script, which is what makes the TUI (and any other
#: front end) demonstrable and testable without a network or an API key.
_NO_KEY_PROVIDERS = frozenset({"ollama", "local", "fake"})

# Global provider instance cache
_provider_instance = None


def get_provider(config: Any = None, *, force: bool = False) -> AbstractProvider:
    """Build (or return the cached) provider for `config`.

    Two defects fixed here:

      * `config` used to be ignored. The function read the module-level
        get_config() singleton, so a caller that had applied `--provider` to its
        own Config still got whatever the singleton said — `xli run --provider
        openai` silently built mistral. Passing a config now works.

      * The cached instance was returned before any of that was consulted, so
        even with the config honoured, a second call with a different provider
        returned the first one. `force=True` rebuilds.
    """
    global _provider_instance

    if config is None:
        config = get_config()

    if _provider_instance is not None and not force:
        return _provider_instance

    provider_name = config.default_provider()

    logger.log_structured("INFO", "providers", f"Creating provider: {provider_name}")

    # module, class, default model. The model is left None where the provider
    # class already carries a sensible default.
    provider_classes = {
        "mistral": ("xli.providers.mistral", "MistralProvider", None),
        "openai": ("xli.providers.openai", "OpenAIProvider", None),
        "anthropic": ("xli.providers.anthropic", "AnthropicProvider", None),
        "claude": ("xli.providers.anthropic", "AnthropicProvider", None),
        "openrouter": ("xli.providers.openrouter", "OpenRouterProvider", None),
        "gemini": ("xli.providers.gemini", "GeminiProvider", None),
        "google": ("xli.providers.gemini", "GeminiProvider", None),
        # No API key needed, so this is the one provider that works out of the
        # box against a local `ollama serve`.
        "ollama": ("xli.providers.ollama", "OllamaProvider", None),
        "local": ("xli.providers.ollama", "OllamaProvider", None),
        "fake": ("xli.providers.fake", "FakeProvider", "fake-model"),
    }

    if provider_name not in provider_classes:
        logger.log_structured("ERROR", "providers", f"Unknown provider: {provider_name}")
        raise ValueError(f"Unknown provider: {provider_name}")

    module_path, class_name, default_model = provider_classes[provider_name]
    api_key = config.api_key(provider_name)
    if not api_key and provider_name not in _NO_KEY_PROVIDERS:
        logger.log_structured("ERROR", "providers", f"{provider_name} API key not found")
        raise ValueError(f"{provider_name.upper()}_API_KEY not set in environment")

    import importlib
    module = importlib.import_module(module_path)
    provider_cls = getattr(module, class_name)
    kwargs = {"api_key": api_key}
    if default_model:
        kwargs["model"] = default_model

    # A configured model and endpoint override the class defaults. Both are
    # keyword-only on OpenAICompatibleProvider, and the providers that are not
    # OpenAI-shaped accept **kwargs, so this is safe for all of them.
    model = config.model()
    if model:
        kwargs["model"] = model
    base_url = config.base_url()
    if base_url:
        kwargs["base_url"] = base_url

    try:
        _provider_instance = provider_cls(**kwargs)
    except TypeError as exc:
        # A provider that takes neither base_url nor model should still build
        # rather than fail the whole run because of an optional setting.
        logger.log_error(
            "providers", f"{provider_name} rejected {sorted(kwargs)}; retrying minimal", exc=exc
        )
        _provider_instance = provider_cls(api_key=api_key)

    logger.log_structured("INFO", "providers", f"Provider ready: {provider_name}")
    return _provider_instance


def reset_provider() -> None:
    """Clear the cached provider instance (needed for tests and provider/key switches)."""
    global _provider_instance
    _provider_instance = None
