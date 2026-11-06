"""Bounded diagnostics. Never log exception messages, headers or payloads."""

import json
import sys
import time
from contextlib import contextmanager
from uuid import UUID


@contextmanager
def diagnostic_stage(stage, envelope):
    started = time.monotonic()
    record = {"event": "maestro_stage_finished", "stage": stage, "outcome": "ok"}
    for key in ("execution_id", "delivery_id"):
        try:
            record[key] = str(UUID(envelope[key]))
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
    try:
        yield
    except BaseException as error:
        record["outcome"] = "error"
        # Class name only: no message, repr, traceback, URL or exception attributes.
        record["exception_type"] = type(error).__name__[:80]
        raise
    finally:
        record["duration_ms"] = round((time.monotonic() - started) * 1000)
        try:
            print(json.dumps(record), file=sys.stderr, flush=True)
        except Exception:
            pass  # Telemetry must not change delivery semantics.
