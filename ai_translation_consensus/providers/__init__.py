# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from . import usage
from .base import BaseProvider, ChatProvider, MTProvider
from .chat import Anthropic, Cohere, Gemini, LocalCommand, NoKeyOpenAI, OpenAICompatible
from .http import ProviderError
from .mt import AzureTranslator, DeepL, GoogleTranslate, Lingva, LibreTranslate, MyMemory

# kind -> class. Tests (and third-party modules) may register more kinds here.
REGISTRY = {cls.kind: cls for cls in (
    OpenAICompatible, NoKeyOpenAI, Anthropic, Gemini, Cohere, LocalCommand,
    DeepL, GoogleTranslate, AzureTranslator, LibreTranslate, MyMemory, Lingva,
)}


def kind_selection():
    return [(k, cls.label) for k, cls in REGISTRY.items()]


def build(kind, cfg):
    try:
        return REGISTRY[kind](cfg)
    except KeyError:
        raise ProviderError("unknown provider kind %r" % kind)
