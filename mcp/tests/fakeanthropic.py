"""Fake anthropic SDK для тестів chat._llm_reply — жодного реального ключа чи
мережевого виклику. Форма об'єктів повторює те, на що спирається briefing.py:
response.content[i].type/.text/.name/.input/.id, response.usage.input_tokens/
output_tokens, response.stop_reason.

chat._llm_reply робить `import anthropic` ЛІНИВО, усередині функції (як і
briefing._llm_brief) — тому підміна sys.modules['anthropic'] ДО виклику
_llm_reply достатня: наступний `import anthropic` у тілі функції поверне вже
підмінений модуль.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(name: str, input_: dict, id_: str = "tool_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=input_, id=id_)


class FakeResponse:
    def __init__(self, content, stop_reason, tokens_in=10, tokens_out=5):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out)


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake anthropic client ran out of scripted responses")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def install(monkeypatch, responses) -> dict:
    """Підміняє sys.modules['anthropic']. Повертає {"client": FakeClient}
    (наповнюється лише після першого anthropic.Anthropic(...) виклику
    всередині _llm_reply) — тест може зазирнути в client.messages.calls."""
    holder: dict = {}

    def Anthropic(api_key=None):
        client = FakeClient(responses)
        holder["client"] = client
        return client

    fake_module = SimpleNamespace(Anthropic=Anthropic)
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    return holder
