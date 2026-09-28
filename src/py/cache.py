"""The parse cache: a decoded session model beside its capture.

Keyed by the capture's SHA-256 and this tool's version, and stored next to the
capture as `<file>.ueiacache` -- never in a global folder, because the model
belongs to one file. The cache may only change how long a command takes: no
command's output may ever say whether it was warm, and a miss is a full parse
that lands in the same place.

$UEI_NO_CACHE=1 turns reading and writing off (and is what the hermetic suite
uses, so no test ever touches a real cache).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from shapes import CACHE_FORMAT, ENV_NO_CACHE, TOOL_VERSION, UeiaError, env_flag

CACHE_SUFFIX = ".ueiacache"
_HASH_CHUNK = 1 << 20


def cache_path(capture: Path) -> Path:
    """Where the cache for one capture lives: beside it, named after it."""
    resolved = capture.resolve()
    return resolved.with_name(resolved.name + CACHE_SUFFIX)


def capture_identity(capture: Path) -> Dict[str, Any]:
    """The capture's size and SHA-256 -- what the cache is keyed by."""
    digest = hashlib.sha256()
    size = 0
    try:
        with open(str(capture), "rb") as stream:
            while True:
                chunk = stream.read(_HASH_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise UeiaError("cannot read capture %s: %s" % (capture.name, exc))
    return {"size": size, "sha256": digest.hexdigest().upper()}


def disabled() -> bool:
    """True when $UEI_NO_CACHE asks for the cache to stay untouched."""
    return env_flag(ENV_NO_CACHE)


def load(capture: Path, identity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The cached model for this exact capture, or None (a miss is not an error)."""
    if disabled():
        return None
    path = cache_path(capture)
    try:
        with open(str(path), "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("cache_format") != CACHE_FORMAT:
        return None
    if document.get("tool_version") != TOOL_VERSION:
        return None
    if document.get("capture_sha256") != identity["sha256"]:
        return None
    if document.get("capture_size") != identity["size"]:
        return None
    model = document.get("model")
    return model if isinstance(model, dict) else None


def store(capture: Path, identity: Dict[str, Any], model: Dict[str, Any]) -> Path:
    """Write the model beside the capture, atomically, unless caching is off."""
    path = cache_path(capture)
    if disabled():
        return path
    document = {
        "cache_format": CACHE_FORMAT,
        "tool_version": TOOL_VERSION,
        "capture_sha256": identity["sha256"],
        "capture_size": identity["size"],
        "model": model,
    }
    temporary = path.with_name(path.name + ".tmp")
    try:
        with open(str(temporary), "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
        os.replace(str(temporary), str(path))
    except OSError as exc:
        # A cache that cannot be written is a slow run, not a failure.
        try:
            os.remove(str(temporary))
        except OSError:
            pass
        del exc
    return path


def status(capture: Path, identity: Dict[str, Any]) -> Dict[str, Any]:
    """What the cache holds for this capture right now (for the cache command)."""
    path = cache_path(capture)
    result: Dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not result["exists"]:
        return result
    try:
        result["bytes"] = path.stat().st_size
    except OSError:
        result["bytes"] = 0
    model = load(capture, identity)
    result["matches"] = model is not None
    if model is not None:
        result["model"] = model
    return result


def clear(capture: Path) -> bool:
    """Remove the cache; True when a file was removed."""
    path = cache_path(capture)
    try:
        path.unlink()
    except OSError:
        return False
    return True
