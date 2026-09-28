"""The LZ4 block decoder the packet layer needs: the C library, loaded with ctypes.

A trace packet larger than 384 bytes may be LZ4 block-compressed (its thread id carries
`ENCODED_MARKER` and a uint16 decoded size follows the header). The format is the raw LZ4
block format -- a sequence of [token][literals][match offset][match length] runs, no frame
header -- so `LZ4_decompress_safe` from the C library decodes it directly.

**The decoder is the C library and nothing else**, on purpose: a packet decoded by anything
laxer is one every command downstream would read as fact, and one decoder means one answer.
`_c_function` looks for `bin/ueia_lz4.dll` (this repository's build, one command away), then
for what the system has; `$UEI_LZ4_DLL` names one explicitly. Nothing found is a refusal that
says how to get the first of those, not a silent fallback.

The library this repository builds has a second job here, and it is the pipeline's first step
(AGENTS.md): `build_state` says whether `bin/ueia_lz4.dll` exists and matches the recipe that
would build it, `self_test` proves it decodes, and `build` is `ueia lz4 --build` -- idempotent,
so a current DLL costs three file hashes and no compiler run.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict

from shapes import Lz4Error

#: The library names to try after `bin/ueia_lz4.dll`: what a system package installs, on Windows,
#: Linux and macOS. `liblz4.so.1` deliberately before `liblz4.so`, which only exists with a -dev
#: package.
_LZ4_LIBS = ("lz4.dll", "liblz4.so.1", "liblz4.so", "liblz4.dylib")

_ENV_DLL = "UEI_LZ4_DLL"
_DLL_NAME = "ueia_lz4.dll"
_STAMP_NAME = "ueia_lz4.build.json"
_STAMP_FORMAT = 1
_CMAKE_CONFIGURE = ("cmake", "-S", ".", "-B", "build")
_CMAKE_BUILD = ("cmake", "--build", "build", "--config", "Release")

#: The one command that builds the library -- what every "no library" and "stale" message points at,
#: and the first step of the pipeline in AGENTS.md.
ENSURE_COMMAND = "python src\\py\\ueia.py lz4 --build"

#: What a stamp must cover: both LZ4 sources and the recipe file itself, so a flag change counts as a
#: change to the library. A stamp that does not cover one of these does not describe the DLL.
RECIPE_SOURCES = ("CMakeLists.txt", "src/cpp/third_party/lz4/lz4.c", "src/cpp/third_party/lz4/lz4.h")

#: A block whose answer is known, for `self_test`: one literal-only LZ4 sequence -- a token of
#: `12 << 4` is "12 literals, no match", which is a legal block on its own -- carrying the payload.
#: Built here rather than imported from the fixtures, so the check shares no code with the tests.
_SELF_TEST_PAYLOAD = b"lz4 selftest"
_SELF_TEST_BLOCK = bytes((len(_SELF_TEST_PAYLOAD) << 4,)) + _SELF_TEST_PAYLOAD


class BuildState(TypedDict):
    """What `bin/ueia_lz4.dll` is, whether the library in use is it, and what is wrong if anything is.

    Two questions, kept apart on purpose: `dll_current` is about the *file* (it exists, it has a
    stamp, the stamp still describes the sources) and is what a build decides on; `current` is
    about the library this process actually loads, so a system liblz4 -- which nothing here built
    -- can never be called stale, only `ours` false.
    """

    dll: str
    exists: bool
    stamped: bool
    stamp: Optional[Dict[str, Any]]
    dll_current: bool
    ours: bool
    library: Optional[str]
    version: Optional[str]
    current: bool
    problems: List[str]


#: The resolved function, the library it came from, and whether the search has run: the library is
#: loaded once per process, not once per packet (the corpus holds 24,767 encoded packets).
_resolved: Optional[Callable[..., int]] = None
_library: Optional[Any] = None
_resolved_name: Optional[str] = None
_searched = False


# --------------------------------------------------------------------------- paths
def repo_root() -> Path:
    """The repository root, which `src/py/lz4.py` sits two levels below."""
    return Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def dll_path(root: Optional[Path] = None) -> Path:
    """`bin/ueia_lz4.dll`: the library this repository builds."""
    return (root or repo_root()) / "bin" / _DLL_NAME


def stamp_path(root: Optional[Path] = None) -> Path:
    """`bin/ueia_lz4.build.json`: what that library was built from (`tools/stamp_lz4.cmake`)."""
    return (root or repo_root()) / "bin" / _STAMP_NAME


# --------------------------------------------------------------------------- loading
def _candidates() -> List[str]:
    """Where the library is looked for, in order: named, ours, then a system package."""
    candidates: List[str] = []
    named = os.environ.get(_ENV_DLL)
    if named:
        candidates.append(named)
    candidates.append(str(dll_path()))
    candidates.extend(_LZ4_LIBS)
    return candidates


def _c_function() -> Optional[Callable[..., int]]:
    """`LZ4_decompress_safe` from whatever library has it, or None.

    A library older than 1.7 has no `decompress_safe` and is refused by the symbol lookup rather
    than by a version check. The signature is set rather than left to ctypes' guessing, which
    would truncate the pointers this call is entirely made of: `(src, dst, srcSize,
    dstCapacity) -> bytes written, negative on anything the library refuses`.

    `ctypes` itself is imported here, not at the module level: a process reads the parse cache
    far more often than it decodes a packet.
    """
    global _resolved, _searched, _library, _resolved_name
    if _searched:
        return _resolved
    import ctypes

    for name in _candidates():
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        candidate = getattr(lib, "LZ4_decompress_safe", None)
        if candidate is None:
            continue
        candidate.restype = ctypes.c_int
        candidate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        _resolved, _library, _resolved_name = candidate, lib, name
        break
    _searched = True
    return _resolved


def _reset() -> None:
    """Forget the resolved library: what a build just replaced, and what the tests point elsewhere."""
    global _resolved, _searched, _library, _resolved_name
    _resolved, _library, _resolved_name, _searched = None, None, None, False


def available() -> bool:
    """True when a library answered -- what the real-library half of the suite skips on."""
    return _c_function() is not None


def library_name() -> Optional[str]:
    """The library that answered, or None: what makes "is this the one this repo builds?" answerable."""
    _c_function()
    return _resolved_name


def library_version() -> Optional[str]:
    """The library's own version string (`LZ4_versionString`), when it exports one."""
    import ctypes

    library = _library
    if library is None:
        return None
    symbol = getattr(library, "LZ4_versionString", None)
    if symbol is None:
        return None
    symbol.restype = ctypes.c_char_p
    value = symbol()
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return None


def _refusal() -> Lz4Error:
    return Lz4Error(
        "no LZ4 library to decode with (tried %s and, failing that, %s): run `%s`, or point $%s at "
        "a library exporting LZ4_decompress_safe"
        % (
            _ENV_DLL,
            ", ".join("%s" % (name,) for name in _candidates()),
            ENSURE_COMMAND,
            _ENV_DLL,
        )
    )


def decompress_block(src: bytes, decoded_size: int) -> bytes:
    """Decode one raw LZ4 block that must produce exactly `decoded_size` bytes.

    The destination is allocated at exactly the declared size, so a block that would decode to
    more is refused by the library's own bounds rather than by a check after the fact, and a
    block that decodes to less is a refusal of ours: a short result returned as if it were whole
    is a packet every command downstream would read as fact.
    """
    import ctypes

    function = _c_function()
    if function is None:
        raise _refusal()
    source = ctypes.create_string_buffer(src)
    destination = ctypes.create_string_buffer(decoded_size)
    written = function(
        ctypes.cast(source, ctypes.c_void_p),
        ctypes.cast(destination, ctypes.c_void_p),
        len(src),
        decoded_size,
    )
    if written < 0:
        raise Lz4Error(
            "the LZ4 library refused a %d-byte block declaring %d decoded bytes"
            % (len(src), decoded_size)
        )
    if written != decoded_size:
        raise Lz4Error(
            "LZ4 block decoded %d bytes where the packet claims %d" % (written, decoded_size)
        )
    return destination.raw[:written]


# --------------------------------------------------------------------------- the built library
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recipe_problems(root: Path, stamp: Dict[str, Any]) -> List[str]:
    """What the stamp gets wrong about `root`'s sources, in the order they are checked.

    A missing stamp, a stamp from another format, a source the stamp does not cover, a source
    whose bytes changed since the build, and a DLL that is not the one the stamp describes are
    all reasons to rebuild -- and naming *which* is the whole point of keeping the stamp.
    """
    problems: List[str] = []
    if stamp.get("stamp_format") != _STAMP_FORMAT:
        problems.append(
            "the stamp is format %r, not %d" % (stamp.get("stamp_format"), _STAMP_FORMAT)
        )
    sources = stamp.get("sources")
    if not isinstance(sources, dict):
        problems.append("the stamp lists no sources")
    else:
        for relative in RECIPE_SOURCES:
            stamped = sources.get(relative)
            if not isinstance(stamped, str):
                problems.append("the stamp does not cover %s" % relative)
                continue
            path = root / relative
            if not path.is_file():
                problems.append("%s is gone" % relative)
            elif _sha256(path) != stamped:
                problems.append("%s changed since the build" % relative)
    return problems


def build_state(root: Optional[Path] = None) -> BuildState:
    """What the library is, whether it is current, and what is wrong if it is not.

    "Current" is a recipe comparison, not a timestamp one: the stamp records the SHA-256 of both
    LZ4 sources and of `CMakeLists.txt` -- so a flag change counts as a change -- plus the DLL's
    own hash. Two things a stamp cannot speak for are reported rather than assumed: a library
    that is not this repository's build (`ours` false: a system liblz4, or one `$UEI_LZ4_DLL`
    names) is never called stale, because nothing here built it; and a library that cannot be
    loaded is `library` None.
    """
    base = root or repo_root()
    dll = dll_path(base)
    state = BuildState(
        dll=str(dll),
        exists=dll.is_file(),
        stamped=False,
        stamp=None,
        dll_current=False,
        ours=False,
        library=None,
        version=None,
        current=False,
        problems=[],
    )
    name = library_name()
    state["library"] = name
    state["version"] = library_version()
    state["ours"] = bool(name) and os.path.normcase(os.path.abspath(name)) == os.path.normcase(
        os.path.abspath(str(dll))
    )

    problems: List[str] = []
    if state["exists"]:
        path = stamp_path(base)
        stamp: Optional[Dict[str, Any]] = None
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems.append("the build stamp is unreadable (%s)" % exc)
            else:
                stamp = loaded if isinstance(loaded, dict) else None
                if stamp is None:
                    problems.append("the build stamp is not an object")
        else:
            problems.append("there is no build stamp beside it (%s)" % _STAMP_NAME)
        if stamp is not None:
            state["stamped"] = True
            state["stamp"] = stamp
            problems.extend(_recipe_problems(base, stamp))
            described = stamp.get("dll_sha256")
            if isinstance(described, str) and _sha256(dll) != described:
                problems.append("the DLL is not the one the stamp describes")
    state["problems"] = problems
    state["dll_current"] = bool(state["exists"] and state["stamped"] and not problems)
    state["current"] = bool(state["ours"] and state["dll_current"])
    return state


def self_test() -> Tuple[bool, str]:
    """Decode a block whose answer is known, through the library in use: is it a working decoder?

    Loading a library and finding a symbol only proves it will be *called*; this proves it decodes
    the block format this tool hands it, on this machine, before a capture is committed to it.
    """
    if _c_function() is None:
        return False, "no library to decode with"
    try:
        decoded = decompress_block(_SELF_TEST_BLOCK, len(_SELF_TEST_PAYLOAD))
    except Lz4Error as exc:
        return False, str(exc)
    if decoded != _SELF_TEST_PAYLOAD:
        return False, "the library decoded %r, not %r" % (decoded, _SELF_TEST_PAYLOAD)
    return True, "%d bytes decoded exactly" % len(decoded)


def build(force: bool = False, run: Optional[Callable[..., Any]] = None,
          root: Optional[Path] = None) -> Tuple[int, List[str]]:
    """Build `bin/ueia_lz4.dll` when it is missing or stale; returns (exit code, lines).

    Idempotent on purpose: this is the pipeline's first step (AGENTS.md), so a run with nothing to
    do has to cost three file hashes and no compiler. `run` is injected by the tests; `root` lets
    them point at a fixture tree.
    """
    base = root or repo_root()
    runner = run or subprocess.run
    state = build_state(base)
    lines: List[str] = []
    if state["dll_current"] and not force:
        lines.append("state    : current, nothing to build")
        return 0, lines
    if not state["exists"]:
        lines.append("state    : %s is missing" % _DLL_NAME)
    elif state["problems"]:
        lines.append("state    : stale (%s)" % "; ".join(state["problems"]))
    elif force:
        lines.append("state    : current, rebuilt because --force said so")
    else:
        lines.append("state    : not the library this repository builds")

    for argv in (_CMAKE_CONFIGURE, _CMAKE_BUILD):
        lines.append("build    : %s" % " ".join(argv))
        try:
            done = runner(list(argv), cwd=str(base), capture_output=True, text=True)
        except OSError as exc:
            lines.append("error    : could not run %s (%s)" % (argv[0], exc))
            return 1, lines
        if done.returncode != 0:
            for stream, text in (("stdout", done.stdout), ("stderr", done.stderr)):
                for line in (text or "").strip().splitlines()[-8:]:
                    lines.append("%-9s: %s" % (stream, line))
            lines.append("error    : %s exited %d" % (" ".join(argv), done.returncode))
            return 1, lines

    _reset()
    after = build_state(base)
    if not after["dll_current"]:
        lines.append("error    : the build ran but the result is not current (%s)"
                     % ("; ".join(after["problems"]) or "no stamp beside the DLL",))
        return 1, lines
    ok, detail = self_test()
    lines.append("built    : %s" % after["dll"])
    if not ok:
        lines.append("error    : the rebuilt library does not decode (%s)" % detail)
        return 1, lines
    return 0, lines
