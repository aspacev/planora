"""An LLM provider behind a thin interface: `generate(messages, schema) -> dict`.

It is thin not for beauty's sake: changing the provider or moving to a local
model must not disturb the rest of the code. All the application knows about
the model is that it takes a list of messages and returns an object matching a
schema.

There is no network in the tests and there must not be: the interview and the
parsing of an answer are checked against recorded model answers, and
`RecordedProvider` is not a stub "so it compiles" but a full implementation of
the same interface.
"""

import json
import urllib.error
import urllib.request
from typing import Protocol


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refusing to follow redirects.

    The LLM address is checked for being public before the request, but the
    check is worth nothing if a public address can answer 302 with an internal
    one: the redirect is then followed with no check at all. A 3xx response
    turns into an HTTPError and is caught as an ordinary model refusal.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_opener = urllib.request.build_opener(_NoRedirects())


class LlmError(Exception):
    """The model did not answer or answered with garbage.

    The conversation and the draft are preserved, and the session continues from
    the same place: a model failure must not cost a person half an hour of
    conversation.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class LlmProvider(Protocol):
    def generate(self, messages: list[dict], schema: dict) -> tuple[dict, int]:
        """The model's answer per the schema, and the number of tokens spent."""
        ...


class HttpProvider:
    """Any service with an OpenAI-compatible `/chat/completions`.

    Compatibility specifically, not "OpenAI": the address and the model come
    from the organization's settings, and the same code works with a local model
    behind llama.cpp or vLLM. urllib instead of a provider's client for the same
    reason: a client would tie to one cloud what was promised to be open.
    """

    def __init__(self, *, base_url: str, model: str, api_key: str, timeout: int):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._key = api_key
        self._timeout = timeout

    def generate(self, messages: list[dict], schema: dict) -> tuple[dict, int]:
        payload = json.dumps(
            {
                "model": self._model,
                "messages": messages,
                # The schema is passed to the model rather than merely checked
                # afterwards: that is cheaper than one extra retry, and the check
                # on our side stays regardless — the model's promises cannot be
                # trusted.
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "planora", "schema": schema, "strict": True},
                },
            }
        ).encode()
        request = urllib.request.Request(
            self._url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
        )
        # The address is checked on every request, not only when settings are
        # saved: the host's DNS record could have changed since (rebinding is a
        # standard SSRF technique). The import is local so as not to create a
        # cycle: netguard raises LlmError from this very module.
        from app.ai.netguard import ensure_public_https

        ensure_public_https(self._url)
        try:
            with _opener.open(request, timeout=self._timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise LlmError("llm_refused", f"модель ответила {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            # A timeout is told apart from an unreachable address: "the model is
            # unreachable" sends a person to check the key and the network, while
            # the model was reached and was still thinking — over a long
            # specification that takes minutes — and the remedy is a longer wait.
            # urllib reports it two ways: raised as is while reading the answer,
            # wrapped in URLError while connecting.
            reason = getattr(error, "reason", error)
            if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
                raise LlmError("llm_timeout", "модель не успела ответить") from error
            raise LlmError("llm_unreachable", str(error)) from error
        except json.JSONDecodeError as error:
            raise LlmError("llm_bad_json", "ответ не разобрать как JSON") from error

        try:
            content = body["choices"][0]["message"]["content"]
            tokens = int(body.get("usage", {}).get("total_tokens", 0))
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise LlmError("llm_bad_shape", "в ответе нет ожидаемых полей") from error

        try:
            return json.loads(content), tokens
        except json.JSONDecodeError as error:
            raise LlmError("llm_bad_json", "модель вернула не JSON") from error


class RecordedProvider:
    """Pre-recorded answers, one per call.

    Everything that is not about the network is checked with them: both a valid
    schema and a broken one. Running out of records is a test error rather than
    model behaviour, and it must look exactly like one.
    """

    def __init__(self, responses: list[dict | Exception], tokens: int = 100):
        self._responses = list(responses)
        self._tokens = tokens
        self.calls: list[list[dict]] = []

    def generate(self, messages: list[dict], schema: dict) -> tuple[dict, int]:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("записанные ответы модели кончились")
        answer = self._responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, self._tokens
