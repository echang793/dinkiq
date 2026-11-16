"""Crash-safe JSON persistence for session artifacts.

Every stage writes its result as a JSON file, and the API re-reads them all on
every request. Two failure modes this module closes:

* A plain write_text() truncates the file first, so a crash or full disk
  mid-write leaves a half-written file -- and readers that called json.loads
  directly meant one bad meta.json made every list endpoint 500 at once.
  atomic_write_json writes a temp file in the same directory, fsyncs it and
  os.replace()s it into place, so readers see the old file or the new one.
* NaN/Inf. json.dumps emits bare `NaN` (invalid JSON; hard-fails iOS Safari)
  and Starlette refuses to serialize it (-> 500). sanitize() maps them to null
  and casts numpy scalars/arrays that would otherwise raise TypeError.
"""

import json
import logging
import math
import os
import tempfile
from pathlib import Path

import numpy as np

log = logging.getLogger("dinkiq.jsonio")


def sanitize(obj):
    """Recursively make `obj` strict-JSON safe: NaN/Inf -> None, numpy ->
    native Python, tuples -> lists. Everything else passes through."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return sanitize(obj.tolist())
    if isinstance(obj, np.generic):
        return sanitize(obj.item())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def dumps(data) -> str:
    """Strict-JSON dump (allow_nan=False after sanitizing, so it cannot emit NaN)."""
    return json.dumps(sanitize(data), allow_nan=False)


def atomic_write_json(path: Path | str, data) -> None:
    """Write `data` as JSON so that `path` is always either the old complete
    file or the new complete file, never a partial one."""
    path = Path(path)
    payload = dumps(data)  # serialize first: a bad value must fail before touching disk
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_json(path: Path | str, default=None, on_corrupt=None):
    """Read a JSON file without ever raising.

    Missing file -> `default` (a fresh {} if not given; silent, since absence is
    normal for stages that haven't run). Unreadable/corrupt file -> `on_corrupt`
    if given else `default`, with a warning so the damage is visible in the log
    instead of taking an endpoint down."""
    path = Path(path)
    fallback = {} if default is None else default
    try:
        text = path.read_text()
    except FileNotFoundError:
        return fallback
    except OSError as e:
        log.warning("cannot read %s: %s", path, e)
        return fallback if on_corrupt is None else on_corrupt
    try:
        return json.loads(text)
    except ValueError as e:
        log.warning("corrupt JSON in %s (%s); treating as unreadable", path, e)
        return fallback if on_corrupt is None else on_corrupt
