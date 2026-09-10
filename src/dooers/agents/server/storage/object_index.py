"""Bucket-stored JSON manifest of object keys written through the storage API.

Kept in the bucket (under the org prefix, covered by the tenant SA's conditioned
grant) so `list` needs no `storage.objects.list` IAM grant and stays isolated.
Updated with optimistic concurrency (read generation, write if-generation-match).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from dooers.agents.server.storage import gcs

logger = logging.getLogger(__name__)

INDEX_MAX_RETRIES = 5


class ObjectIndex:
    def __init__(self, bucket: str, index_path: str) -> None:
        self._bucket = bucket
        self._path = index_path

    def _mutate(self, fn: Callable[[dict], None]) -> None:
        # Index maintenance is best-effort and must NEVER raise out of put()/delete():
        # the object write/delete already succeeded by the time this runs, so a read or
        # write error here (gcs.read_json_with_generation deliberately raises on genuine
        # GCS errors, to avoid mistaking a transient failure for "index absent") must log
        # and give up quietly rather than fail the caller's operation.
        for attempt in range(INDEX_MAX_RETRIES):
            try:
                data, gen = gcs.read_json_with_generation(self._bucket, self._path)
                fn(data)
                if gcs.write_json_if_generation(self._bucket, self._path, data, gen):
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning("object index mutation failed for %s: %s", self._path, e)
                return
            if attempt < INDEX_MAX_RETRIES - 1:
                # Small jittered backoff so concurrent writers to the single shared index
                # object don't all retry in lockstep.
                time.sleep(random.uniform(0, 0.05 * (attempt + 1)))
        logger.warning("object index write lost race after %d retries: %s", INDEX_MAX_RETRIES, self._path)

    def record(self, key: str, size: int, content_type: str | None) -> None:
        def _apply(data: dict) -> None:
            data[key] = {
                "size": size,
                "content_type": content_type,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

        self._mutate(_apply)

    def remove(self, key: str) -> None:
        self._mutate(lambda data: data.pop(key, None))

    def entries(self, prefix: str = "") -> list[dict]:
        data, _ = gcs.read_json_with_generation(self._bucket, self._path)
        # Skip a corrupt/non-dict manifest value so one bad entry can't break list()
        # for the whole org.
        out = [{"key": k, **v} for k, v in data.items() if k.startswith(prefix) and isinstance(v, dict)]
        return sorted(out, key=lambda e: e["key"])
