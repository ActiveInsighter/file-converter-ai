"""Provider adapters for the page-transcription model call.

The converter used to speak exactly one wire protocol: Google's
``generativelanguage`` ``generateContent``. Everything protocol-specific now
lives behind :class:`Provider` -- the URL, the auth header, the request body,
the response shape, and how a non-200 answer is classified -- so a second
gateway is a new class instead of a second branch inside the retry loop.

Request content travels in the neutral ``parts`` shape the pipeline already
builds::

    {"text": "..."}                                      a text block
    {"inlineData": {"mimeType": ..., "data": <base64>}}  one page image
    ... plus an optional "mediaResolution" hint per image

Each provider renders that into its own wire format, and translates the
provider-specific answer back into plain Markdown or a :class:`ProviderRefusal`.

Adding another OpenAI-compatible gateway is one instance of
:class:`OpenAICompatibleProvider` in :data:`PROVIDERS`.
"""

from __future__ import annotations

import abc
import email.utils
import json
import re
import time
from typing import ClassVar

from quota_client import classify_quota_error, retry_after_seconds

#: Finish reasons that mean "this page image was refused". Resending the same
#: image reproduces the verdict, so these must never enter the backoff rounds.
BLOCKED_FINISH_REASONS = frozenset(
    {
        "RECITATION",
        "SAFETY",
        "PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
        "IMAGE_SAFETY",
        "LANGUAGE",
        "CONTENT_FILTER",
    }
)

#: HTTP statuses a retry cannot fix: bad request, bad key, no permission, no
#: credit. Gemini only ever answers 400/401/403 in this class, so its own set is
#: narrower -- widening it for Gemini would change how existing runs behave.
PERMANENT_HTTP_STATUSES = frozenset({400, 401, 402, 403, 405, 422})

#: A gateway reports a dead or unfunded account as HTTP 429 with one of these
#: codes. Treating them as a transient rate limit would burn every retry round
#: of every page on a key that can never succeed, so they are classified as
#: permanent instead.
PERMANENT_GATEWAY_CODES = frozenset(
    {
        "insufficient_quota",
        "insufficient_balance",
        "billing_hard_limit_reached",
        "account_deactivated",
        "invalid_api_key",
        "no_credit",
    }
)

#: Reasoning effort accepted by OpenAI-compatible gateways.
REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})

#: Gemini thinking level -> OpenAI-compatible reasoning effort.
_EFFORT_ALIASES = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}


class ProviderRefusal(RuntimeError):
    """A well-formed answer that carries no usable transcription.

    ``kind`` is:

    ``"blocked"``
        the provider refused the page image (content policy).
    ``"empty"``
        the provider stopped without emitting any text.
    ``"truncated"``
        the answer ran into the provider's output limit, so it is incomplete.

    None of the three is fixed by resending the same request, so the caller must
    keep them out of the backoff rounds.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _header_retry_after(headers: object) -> float | None:
    """Read a standard ``Retry-After`` header (seconds or HTTP date)."""
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(raw).timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _compact(payload: object) -> str:
    """Flatten any JSON-ish payload into lowercase alphanumeric words."""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return re.sub(r"[^a-z0-9]+", " ", text.lower())


class Provider(abc.ABC):
    """Everything about one gateway that is not retry policy."""

    #: Stable id used by ``--provider``.
    name: ClassVar[str] = ""
    #: Human label used in log lines and error messages.
    label: ClassVar[str] = ""
    #: Base URL used when ``--api-base`` is not given.
    default_base_url: ClassVar[str] = ""
    #: Environment variable that holds the API key(s), one per line.
    api_key_env: ClassVar[str] = ""
    #: Environment variable that supplies the fallback chain, if any.
    fallbacks_env: ClassVar[str] = ""
    #: How to obtain the key, printed when it is missing.
    api_key_hint: ClassVar[str] = ""
    #: Whether the run needs the shared Valkey quota pool. Only the Gemini
    #: free tier does: it is billed per Google Cloud project, so keys must be
    #: paced globally. A paid gateway needs no such coordination.
    uses_shared_quota_pool: ClassVar[bool] = False
    #: Fallback chain used when neither the CLI nor the environment sets one.
    default_model_fallbacks: ClassVar[tuple[str, ...]] = ()
    #: Model id used when ``--model`` is empty. Blank means the caller must
    #: choose, so that switching provider can never silently keep another
    #: gateway's model id.
    default_model: ClassVar[str] = ""
    #: Example model id, used in the error message when ``--model`` is missing.
    model_example: ClassVar[str] = ""
    #: Extra CLI flags this provider understands, for the help text.
    notes: ClassVar[str] = ""
    permanent_statuses: ClassVar[frozenset[int]] = PERMANENT_HTTP_STATUSES

    @abc.abstractmethod
    def endpoint(self, base_url: str, model: str) -> str:
        """Full request URL for one model."""

    @abc.abstractmethod
    def build_headers(self, api_key: str) -> dict[str, str]:
        """Auth and content headers. Never logged."""

    @abc.abstractmethod
    def build_payload(
        self,
        model: str,
        parts: list[dict],
        *,
        thinking_level: str = "",
        reasoning_effort: str = "",
        max_output_tokens: int = 0,
    ) -> dict:
        """Render the neutral content into this gateway's request body."""

    @abc.abstractmethod
    def extract_text(self, payload: dict) -> str:
        """Return the answer text, or raise :class:`ProviderRefusal`."""

    def quota_kind(self, payload: object) -> str:
        """Which quota the gateway says was hit: rpm / rpd / spend / unknown."""
        return "unknown"

    def retry_after(self, headers: object, payload: object) -> float | None:
        """Seconds the gateway asked us to wait, if it said so."""
        return _header_retry_after(headers)

    def status_error_kind(self, status: int, payload: object = None) -> str:
        """Classify a non-200 answer: rate_limited / permanent / unavailable.

        Anything else is returned as ``"retryable"``.
        """
        if status == 429:
            return "rate_limited"
        if status in self.permanent_statuses:
            return "permanent"
        if status == 404 or 500 <= status < 600:
            return "unavailable"
        return "retryable"


class GeminiProvider(Provider):
    """Google AI Studio / Generative Language API."""

    name = "gemini"
    label = "Gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta/models"
    api_key_env = "GEMINI_API_KEYS"
    fallbacks_env = "GEMINI_MODEL_FALLBACKS"
    api_key_hint = "Add a repository Actions secret with one Gemini API key per line."
    uses_shared_quota_pool = True
    default_model_fallbacks = ("gemini-flash-lite-latest",)
    default_model = "gemini-3.5-flash-lite"
    model_example = "gemini-3.5-flash-lite"
    # Narrower than the shared default: 402/405/422 never come back from Google,
    # and reclassifying them would silently change existing runs.
    permanent_statuses = frozenset({400, 401, 403})
    notes = "--thinking-level, --media-resolution"

    def endpoint(self, base_url: str, model: str) -> str:
        return f"{base_url.rstrip('/')}/{model}:generateContent"

    def build_headers(self, api_key: str) -> dict[str, str]:
        return {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    def build_payload(
        self,
        model: str,
        parts: list[dict],
        *,
        thinking_level: str = "",
        reasoning_effort: str = "",
        max_output_tokens: int = 0,
    ) -> dict:
        generation_config: dict = {
            "thinkingConfig": {"thinkingLevel": thinking_level},
        }
        if max_output_tokens > 0:
            generation_config["maxOutputTokens"] = max_output_tokens
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }

    def extract_text(self, payload: dict) -> str:
        texts = []
        for candidate in payload.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                if part.get("thought"):
                    continue
                if part.get("text"):
                    texts.append(part["text"])

        result = "\n".join(texts).strip()
        if not result:
            finish_reasons = [
                candidate.get("finishReason")
                for candidate in payload.get("candidates", [])
                if candidate.get("finishReason")
            ]
            feedback = payload.get("promptFeedback") or {}
            blocked = [reason for reason in finish_reasons if reason in BLOCKED_FINISH_REASONS]
            detail = f"finishReasons={finish_reasons}, promptFeedback={feedback}"
            if feedback.get("blockReason"):
                raise ProviderRefusal(
                    "blocked",
                    f"Gemini blocked this page image: blockReason={feedback['blockReason']}, {detail}",
                )
            if blocked:
                raise ProviderRefusal("blocked", f"Gemini refused this page image. {detail}")
            raise ProviderRefusal("empty", f"Gemini returned no answer text. {detail}")

        # MAX_TOKENS is deliberately not treated as truncation here: Gemini's
        # ceiling has never been reached by this pipeline, and reclassifying it
        # would turn pages that used to pass into failures.
        return result

    def quota_kind(self, payload: object) -> str:
        return classify_quota_error(payload)

    def retry_after(self, headers: object, payload: object) -> float | None:
        return retry_after_seconds(headers, payload)


class OpenAICompatibleProvider(Provider):
    """Any gateway that speaks the OpenAI Chat Completions contract.

    Modelflare (``https://modelflare.dev/v1``) is one instance. Images travel as
    ordinary OpenAI content parts with a base64 ``data:`` URL, and reasoning
    control is the ``reasoning_effort`` field.

    Two gateway-specific decisions worth knowing:

    * ``reasoning_effort`` is **omitted** unless it is asked for, because models
      without explicit reasoning reject the field outright. ``--thinking-level``
      is therefore ignored for this provider; pass ``--reasoning-effort``.
    * ``max_tokens`` is omitted unless ``--max-output-tokens`` is set. A missing
      cap means the gateway default applies, which can be far below what a dense
      page needs, so a truncated answer is reported instead of silently accepted.
    """

    name = "modelflare"
    label = "Modelflare"
    default_base_url = "https://modelflare.dev/v1"
    api_key_env = "MODELFLARE_API_KEYS"
    fallbacks_env = "MODELFLARE_MODEL_FALLBACKS"
    api_key_hint = (
        "Add a repository Actions secret MODELFLARE_API_KEYS with one "
        "Modelflare key (sk-mf-...) per line. The model group -- e.g. "
        "openai-award for the cheap route or openai-stable for the reliable one "
        "-- is granted to the key itself, not to the request."
    )
    uses_shared_quota_pool = False
    default_model_fallbacks = ()
    # Deliberately blank: which vision model is strongest changes faster than
    # this file does, and guessing would silently pick one for the user.
    default_model = ""
    model_example = "gpt-5.6-sol"
    notes = "--reasoning-effort, --max-output-tokens, --api-base"

    def endpoint(self, base_url: str, model: str) -> str:
        return f"{base_url.rstrip('/')}/chat/completions"

    def build_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Modelflare is fronted by Cloudflare; an ordinary browser-ish agent
            # keeps the edge from answering an interstitial instead of JSON.
            "User-Agent": "file-converter-ai/1.0",
        }

    def build_payload(
        self,
        model: str,
        parts: list[dict],
        *,
        thinking_level: str = "",
        reasoning_effort: str = "",
        max_output_tokens: int = 0,
    ) -> dict:
        content: list[dict] = []
        for part in parts:
            if "text" in part:
                content.append({"type": "text", "text": part["text"]})
                continue
            inline = part.get("inlineData")
            if not inline:
                continue
            mime_type = inline.get("mimeType") or "image/png"
            image: dict = {
                "url": f"data:{mime_type};base64,{inline.get('data', '')}",
            }
            detail = _image_detail(part)
            if detail is not None:
                image["detail"] = detail
            content.append({"type": "image_url", "image_url": image})

        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
        effort = normalize_reasoning_effort(reasoning_effort)
        if effort:
            payload["reasoning_effort"] = effort
        if max_output_tokens > 0:
            payload["max_tokens"] = max_output_tokens
        return payload

    def extract_text(self, payload: dict) -> str:
        choices = payload.get("choices") or []
        texts: list[str] = []
        finish_reasons: list[str] = []
        refusals: list[str] = []

        for choice in choices:
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                if content.strip():
                    texts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") in (None, "text") and item.get("text"):
                        texts.append(item["text"])
            if message.get("refusal"):
                refusals.append(str(message["refusal"]))
            if choice.get("finish_reason"):
                finish_reasons.append(str(choice["finish_reason"]).lower())

        detail = f"finishReasons={finish_reasons}"
        result = "\n".join(texts).strip()
        if refusals or any(reason.upper() in BLOCKED_FINISH_REASONS for reason in finish_reasons):
            raise ProviderRefusal(
                "blocked",
                f"{self.label} refused this page image. refusals={refusals}, {detail}",
            )
        if "length" in finish_reasons:
            raise ProviderRefusal(
                "truncated",
                f"{self.label} stopped at its output limit, so the answer is "
                f"incomplete ({detail}); raise --max-output-tokens and retry. "
                "Refusing the page is safer than emitting half of it.",
            )
        if not result:
            raise ProviderRefusal(
                "empty", f"{self.label} returned no answer text. {detail}"
            )
        return result

    def status_error_kind(self, status: int, payload: object = None) -> str:
        if status == 429 and _is_permanent_gateway_error(payload):
            return "permanent"
        return super().status_error_kind(status, payload)

    def quota_kind(self, payload: object) -> str:
        compact = _compact(payload)
        if "per day" in compact or "daily" in compact or "rpd" in compact:
            return "rpd"
        if "per minute" in compact or "rpm" in compact or "rate limit" in compact:
            return "rpm"
        if "balance" in compact or "credit" in compact or "quota" in compact:
            return "spend"
        return "unknown"

    def retry_after(self, headers: object, payload: object) -> float | None:
        candidates = []
        header_value = _header_retry_after(headers)
        if header_value is not None:
            candidates.append(header_value)
        # Some gateways put the wait in the body instead of a header.
        if isinstance(payload, dict):
            for key in ("retry_after", "retryAfter", "retry_after_seconds"):
                raw = payload.get(key)
                if isinstance(raw, bool):
                    continue
                if isinstance(raw, (int, float)):
                    candidates.append(max(0.0, float(raw)))
                elif isinstance(raw, str) and raw.strip().replace(".", "", 1).isdigit():
                    candidates.append(max(0.0, float(raw.strip())))
        return max(candidates) if candidates else None


def _image_detail(part: dict) -> str | None:
    """Map the pipeline's ``mediaResolution`` hint onto OpenAI ``detail``.

    Transcription wants the most pixels the gateway will give us, so anything
    above ``low`` becomes ``high``; ``unspecified`` omits the field entirely.
    """
    level = ((part.get("mediaResolution") or {}).get("level") or "")
    level = level.rsplit("_", 1)[-1].lower()
    if not level or level == "unspecified":
        return None
    return "low" if level == "low" else "high"


def _is_permanent_gateway_error(payload: object) -> bool:
    """True when a 429 actually means "this key can never work"."""
    compact = _compact(payload)
    if any(code.replace("_", " ") in compact for code in PERMANENT_GATEWAY_CODES):
        return True
    return any(code in compact.replace(" ", "") for code in PERMANENT_GATEWAY_CODES)


def normalize_reasoning_effort(raw: str | None) -> str:
    """Validate ``--reasoning-effort``; empty means "let the gateway decide".

    A Gemini ``--thinking-level`` is a useful shorthand for the same knob, so it
    is accepted and translated instead of being rejected.
    """
    value = (raw or "").strip().lower()
    if not value or value == "unspecified":
        return ""
    if value in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[value]
    if value in REASONING_EFFORTS:
        return value
    raise ValueError(
        f"Unknown reasoning effort {raw!r}; expected one of "
        f"{', '.join(sorted(REASONING_EFFORTS))}"
    )


GEMINI = GeminiProvider()
MODELFLARE = OpenAICompatibleProvider()

#: Every provider selectable with ``--provider``.
PROVIDERS: dict[str, Provider] = {
    GEMINI.name: GEMINI,
    MODELFLARE.name: MODELFLARE,
}

DEFAULT_PROVIDER_NAME = GEMINI.name


def provider_names() -> tuple[str, ...]:
    return tuple(sorted(PROVIDERS))


def resolve_provider(name: str | None) -> Provider:
    """Look up a provider by id, defaulting to Gemini when nothing is set."""
    key = (name or "").strip().lower() or DEFAULT_PROVIDER_NAME
    try:
        return PROVIDERS[key]
    except KeyError:
        raise ValueError(
            f"Unknown provider {name!r}; expected one of {', '.join(provider_names())}"
        ) from None


def resolve_model(provider: Provider, requested: str | None) -> str:
    """Pick the model id, falling back to the provider's own default.

    Resolving this per provider is what stops a provider switch from silently
    carrying the previous gateway's model id into every request.
    """
    model = (requested or "").strip()
    if model:
        return model
    if provider.default_model:
        return provider.default_model
    raise ValueError(
        f"--model is required for provider {provider.name}; "
        f"{provider.label} model ids look like {provider.model_example or '<vendor>-<model>'}"
    )
