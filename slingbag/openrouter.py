"""OpenRouter-koppeling voor de hulpknop: spraak naar tekst, gesprek (met tools) en tekst naar spraak.

Alleen de standaardbibliotheek, zodat er op de Pi niets extra geïnstalleerd hoeft te worden.

Privacy: elk verzoek vraagt OpenRouter om alleen aanbieders te gebruiken die niets bewaren en
niet met de gegevens trainen (``zdr`` en ``data_collection: deny``). OpenRouter en de aanbieder
verwerken het verzoek wel, maar bewaren het niet. Alles wat bewaard wordt, staat op de Pi.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

log = logging.getLogger(__name__)

API = "https://openrouter.ai/api/v1"
TTS_RATE = 24000  # de spraakmodellen leveren 16-bit mono PCM op 24 kHz


class AIUnavailable(RuntimeError):
    """Geen sleutel, geen internet of een fout bij de aanbieder."""


class KeyStore:
    """Bewaart de API-sleutel in een eigen bestand (alleen leesbaar voor de service), los van config.json.

    De begeleidersapp kan de sleutel instellen, maar krijgt hem nooit terug.
    """

    def __init__(self, path: Path, fallback: str | None = None):
        self.path = Path(path)
        self.fallback = (fallback or "").strip()

    def get(self) -> str:
        try:
            key = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        return key or self.fallback

    def set(self, key: str) -> None:
        key = key.strip()
        if not key:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(key + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def summary(self) -> dict:
        key = self.get()
        return {"ingesteld": bool(key), "eind": key[-4:] if key else None}


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None


class OpenRouterClient:
    def __init__(self, keys: KeyStore, settings: Callable[[], dict], timeout: float = 30):
        self.keys = keys
        self.settings = settings  # geeft de actuele hulpknop-instellingen
        self.timeout = timeout

    # --- algemeen ----------------------------------------------------------------

    def available(self) -> bool:
        return bool(self.keys.get())

    def _provider(self) -> dict:
        provider = {"data_collection": "deny"}
        if self.settings().get("alleen_zdr", True):
            provider["zdr"] = True
        return provider

    def _request(self, path: str, body: dict, timeout: float | None = None):
        key = self.keys.get()
        if not key:
            raise AIUnavailable("Er is nog geen OpenRouter-sleutel ingesteld.")
        body = {**body, "provider": self._provider()}
        req = urllib.request.Request(
            API + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "X-Title": "Slingbag hulpknop"},
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", errors="replace")
            raise AIUnavailable(f"OpenRouter gaf fout {exc.code}: {detail}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise AIUnavailable(f"Geen verbinding met OpenRouter ({exc}).") from exc

    def check(self) -> dict:
        """Controleert de sleutel zonder kosten (voor de knop 'Test verbinding' in de app)."""
        key = self.keys.get()
        if not key:
            raise AIUnavailable("Er is nog geen OpenRouter-sleutel ingesteld.")
        req = urllib.request.Request(API + "/key", headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp).get("data", {})
        except urllib.error.HTTPError as exc:
            raise AIUnavailable("De sleutel wordt niet geaccepteerd." if exc.code in (401, 403)
                                else f"OpenRouter gaf fout {exc.code}.") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise AIUnavailable(f"Geen verbinding met OpenRouter ({exc}).") from exc
        return {"limiet": data.get("limit"), "verbruikt": data.get("usage")}

    # --- spraak naar tekst ------------------------------------------------------------

    def transcribe(self, wav: bytes, language: str = "nl") -> str:
        body = {
            "model": self.settings()["transcriptiemodel"],
            "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"},
            "language": language,
        }
        with self._request("/audio/transcriptions", body, timeout=30) as resp:
            return (json.load(resp).get("text") or "").strip()

    # --- tekst naar spraak --------------------------------------------------------------

    def speech(self, text: str) -> bytes:
        """Geeft 16-bit mono PCM op 24 kHz."""
        settings = self.settings()
        body = {
            "model": settings["stemmodel"],
            "input": text,
            "voice": settings["online_stem"],
            "response_format": "pcm",
            "speed": settings.get("spreeksnelheid", 0.9),
        }
        with self._request("/audio/speech", body, timeout=30) as resp:
            return resp.read()

    # --- gesprek -------------------------------------------------------------------------

    def chat(self, messages: list[dict], max_tokens: int = 1500, json_mode: bool = False,
             model: str | None = None, effort: str = "medium") -> str:
        body = {"model": model or self.settings()["model"], "messages": messages, "max_tokens": max_tokens,
                "reasoning": {"effort": effort}}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        with self._request("/chat/completions", body, timeout=60) as resp:
            data = json.load(resp)
        try:
            return data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError) as exc:
            raise AIUnavailable(f"Onverwacht antwoord van OpenRouter: {str(data)[:200]}") from exc

    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                    on_text: Callable[[str], None] | None = None,
                    should_stop: Callable[[], bool] | None = None,
                    max_tokens: int = 1000) -> ChatResult:
        """Streamt het antwoord; ``on_text`` krijgt elk stukje tekst zodra het binnenkomt."""
        settings = self.settings()
        # Nadenken kost tijd vóór het eerste woord; 'low' houdt het gesprek vlot, maar laat het model
        # bij lastige situaties nog steeds even nadenken.
        body = {"model": settings["model"], "messages": messages, "stream": True,
                "max_tokens": max_tokens, "reasoning": {"effort": settings.get("denkniveau", "low")}}
        if tools:
            body["tools"] = tools
        began = time.monotonic()
        resp = self._request("/chat/completions", body, timeout=30)
        log.info("Model %s antwoordt na %.2f s", body["model"], time.monotonic() - began)
        try:
            return parse_stream(_lines(resp), on_text, should_stop)
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise AIUnavailable(f"Verbinding met OpenRouter onderbroken ({exc}).") from exc
        finally:
            resp.close()


def _lines(resp) -> Iterator[str]:
    import os
    debug = os.environ.get("SLINGBAG_DEBUG_STREAM")
    began = time.monotonic()
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if debug and line:
            log.info("stream +%.2f %s", time.monotonic() - began, line[-220:])
        yield line


def parse_stream(lines, on_text=None, should_stop=None) -> ChatResult:
    """Leest server-sent events in het OpenAI-formaat en voegt de tool-aanroepen samen."""
    result = ChatResult()
    calls: dict[int, dict] = {}
    for line in lines:
        if should_stop and should_stop():
            result.finish_reason = "onderbroken"
            break
        if not line.startswith("data:"):
            continue  # commentaarregels zoals ': OPENROUTER PROCESSING'
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if "error" in event:
            raise AIUnavailable(f"Fout tijdens het antwoord: {event['error']}")
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                result.text += text
                if on_text:
                    on_text(text)
            for call in delta.get("tool_calls") or []:
                slot = calls.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
                slot["id"] = call.get("id") or slot["id"]
                fn = call.get("function") or {}
                slot["name"] = fn.get("name") or slot["name"]
                slot["arguments"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                result.finish_reason = choice["finish_reason"]
    result.tool_calls = [
        {"id": c["id"] or f"call_{i}", "type": "function",
         "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
        for i, c in sorted(calls.items()) if c["name"]
    ]
    return result


def pcm_to_wav(pcm: bytes, path: Path, rate: int = TTS_RATE) -> None:
    import wave
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
