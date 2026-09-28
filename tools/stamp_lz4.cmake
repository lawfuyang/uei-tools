# Writes bin/ueia_lz4.build.json: what the DLL beside it was built from, and the DLL's own hash.
#
# Called by CMakeLists.txt as a POST_BUILD step, never by hand. `ueia lz4 --check` reads it to answer
# "is the library this checkout uses the library this checkout's sources build?" -- hashing the recipe
# (the two LZ4 sources and CMakeLists.txt, so a flag change counts as a change) rather than comparing
# timestamps, which say nothing about *what* changed. `ueia lz4 --build` rebuilds only when the answer
# is no.
#
# Arguments: DLL (the built library), OUT (the stamp to write), SOURCE_DIR (the repository root),
# FLAGS (the recipe's compile definitions, comma-separated).

if(NOT EXISTS "${DLL}")
  message(FATAL_ERROR "stamp_lz4.cmake: no library at '${DLL}'")
endif()

file(SHA256 "${SOURCE_DIR}/src/cpp/third_party/lz4/lz4.c" _lz4_c)
file(SHA256 "${SOURCE_DIR}/src/cpp/third_party/lz4/lz4.h" _lz4_h)
file(SHA256 "${SOURCE_DIR}/CMakeLists.txt" _recipe)
file(SHA256 "${DLL}" _dll)
file(SIZE "${DLL}" _size)
string(REPLACE "," " " _flags "${FLAGS}")
string(TIMESTAMP _built "%Y-%m-%dT%H:%M:%SZ" UTC)

file(WRITE "${OUT}" "{
  \"stamp_format\": 1,
  \"library\": \"LZ4 v1.9.2\",
  \"flags\": \"${_flags}\",
  \"compiler\": \"${COMPILER_ID} ${COMPILER_VERSION}\",
  \"cmake\": \"${CMAKE_VERSION}\",
  \"built_utc\": \"${_built}\",
  \"dll\": \"bin/ueia_lz4.dll\",
  \"dll_size\": ${_size},
  \"dll_sha256\": \"${_dll}\",
  \"sources\": {
    \"CMakeLists.txt\": \"${_recipe}\",
    \"src/cpp/third_party/lz4/lz4.c\": \"${_lz4_c}\",
    \"src/cpp/third_party/lz4/lz4.h\": \"${_lz4_h}\"
  }
}
")
