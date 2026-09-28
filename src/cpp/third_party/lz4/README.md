# LZ4 v1.9.2 — https://github.com/lz4/lz4/releases/tag/v1.9.2

The decoder `CMakeLists.txt` builds as `bin/ueia_lz4.dll` and the offline tool loads with ctypes
(`REFERENCE.md` §2). Vendored rather than fetched, so a build needs no network.

* `lz4.c`, `lz4.h` — the library, verbatim. Copied from the sibling
  [rdc-tools](https://github.com/lawfuyang/rdc-tools) repository's `src/cpp/third_party/lz4`,
  which took them from the replay engine's own 3rdparty tree. This engine's TraceLog vendors the
  same upstream version (1.9.2) for the same job: compressing the very packets this decoder reads.
* `LICENSE` — the upstream BSD-2-Clause text, kept beside the source.
