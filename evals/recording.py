"""Replay-mode `Transport` for the golden-ask suite (spec §9 Evals).

Default runs (`make test`) replay canned model outputs from
`evals/fixtures/golden_recordings.json` keyed by a hash of the exact prompt
sent to the transport — zero network, fully deterministic. A missing
recording fails the test loudly rather than silently falling through to a
real call.

Refreshing recordings requires `-m live_record` (a marker registered and
deselected by default, same as `live`): under that marker, `RecordingTransport`
wraps a real transport, calls it, and persists the response back to the
fixture file — so recordings are only ever produced by actually running the
suite against the live model, never hand-edited.
"""

import json
from pathlib import Path
from typing import Any

from core.catalog.hashing import content_hash
from core.gateway.transport import Transport, TransportResult

RECORDINGS_PATH = Path(__file__).resolve().parent / "fixtures" / "golden_recordings.json"


def _prompt_key(prompt: str) -> str:
    return content_hash(prompt)


def load_recordings() -> dict[str, Any]:
    if not RECORDINGS_PATH.exists():
        return {}
    return json.loads(RECORDINGS_PATH.read_text())


def save_recordings(recordings: dict[str, Any]) -> None:
    RECORDINGS_PATH.write_text(json.dumps(recordings, indent=2, sort_keys=True) + "\n")


class RecordingTransport:
    """A `Transport` that replays `golden_recordings.json` by default, or (in
    record mode) calls through to a real transport and persists what it
    returns. One instance is normally scoped to a single ask/test; `label`
    identifies that ask in both the recordings file and the "missing
    recording" failure message.
    """

    def __init__(self, *, label: str, real_transport: Transport | None = None, record: bool = False) -> None:
        self._label = label
        self._real_transport = real_transport
        self._record = record
        self._recordings = load_recordings()
        self.calls: list[str] = []

    def complete(self, prompt: str, *, model: str, max_tokens: int, timeout_s: float) -> TransportResult:
        self.calls.append(prompt)
        key = _prompt_key(prompt)

        if self._record:
            if self._real_transport is None:
                raise RuntimeError("RecordingTransport(record=True) requires a real_transport")
            result = self._real_transport.complete(prompt, model=model, max_tokens=max_tokens, timeout_s=timeout_s)
            self._recordings[key] = {
                "label": self._label,
                "model": model,
                "prompt": prompt,
                "response": result.model_dump(),
            }
            save_recordings(self._recordings)
            return result

        entry = self._recordings.get(key)
        if entry is None:
            raise AssertionError(
                f"no recording for ask {self._label!r} (prompt hash {key}) — "
                "refresh with `pytest -m live_record`"
            )
        return TransportResult(**entry["response"])
