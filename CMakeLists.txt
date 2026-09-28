# ueia_lz4.dll — the LZ4 block decoder the offline tool loads with ctypes (REFERENCE §2).
#
#   python src\py\ueia.py lz4 --build   # the pipeline step that runs the two below, and only when stale
#   cmake -S . -B build                 # configure
#   cmake --build build --config Release
#
# The DLL goes to bin/ (gitignored, as is the build tree): `bin/ueia_lz4.dll` is what
# `src/py/lz4.py` looks for first, and it does not move when the build system or its flags change.

cmake_minimum_required(VERSION 3.20)

project(uei_tools LANGUAGES C)

# bin/ holds the DLL; the build tree stays out of the way. A multi-config generator (the Visual Studio
# one) appends the configuration to the output directory, so every configuration is set explicitly:
# `bin\ueia_lz4.dll` is what the loader and the docs call, and it must not become
# `bin\Release\ueia_lz4.dll` when the generator changes.
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/bin")
foreach(_config IN ITEMS DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
  set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${_config} "${CMAKE_CURRENT_SOURCE_DIR}/bin")
endforeach()

# --- the LZ4 decoder the offline tool loads with ctypes ------------------------------------------------
# A trace's EncodedMarker packets are raw LZ4 blocks (REFERENCE §2), and this is what the offline tool
# decodes them with: `lz4.decompress_block` looks for a library exporting `LZ4_decompress_safe` --
# `bin/ueia_lz4.dll` first, then whatever the system has (`$UEI_LZ4_DLL` names one) -- and raises if
# there is none, because the tool has no decoder of its own and a packet decoded by anything laxer is
# one every command downstream would read as fact.
#
# Measured on the corpus (`editor-pie-1`): 0.60 s for its 24,767 encoded packets -- 34.8 MB on the wire,
# 49.9 MB decoded -- which is the whole LZ4 cost of a cold full decode (REFERENCE §6 has the phase table).
#
# The source is vendored under src/cpp/third_party/lz4 (LZ4 v1.9.2, BSD-2-Clause, LICENSE beside it).
# `LZ4_DLL_EXPORT` is what makes `LZ4LIB_API` `__declspec(dllexport)` -- without it the DLL exports
# nothing at all and the loader finds no function in it. `LZ4_FAST_DEC_LOOP` is on because 1.9.2 gates
# it on `__i386__`/`__x86_64__`, which MSVC does not define, so an MSVC build would decode without the
# fast loop upstream enables on x64 everywhere else (1.9.3 keys it on `_M_X64` instead). The decode is
# byte-identical either way; the flag only picks the loop shape.
add_library(ueia_lz4 SHARED "${CMAKE_CURRENT_SOURCE_DIR}/src/cpp/third_party/lz4/lz4.c")
target_include_directories(ueia_lz4 PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src/cpp/third_party/lz4")
target_compile_definitions(ueia_lz4 PRIVATE LZ4_DLL_EXPORT=1 LZ4_FAST_DEC_LOOP=1)
set_target_properties(ueia_lz4 PROPERTIES PREFIX "")
if(MSVC)
  # /W3 and no /WX: it is not this project's code to hold to its own warning rules.
  target_compile_options(ueia_lz4 PRIVATE /W3 /utf-8)
endif()

# --- the build stamp the tool checks before it uses the DLL -------------------------------------------
# `bin/ueia_lz4.build.json` records the recipe (both LZ4 sources and this file, hashed, so a flag change
# counts) and the DLL's own hash, so `ueia lz4 --check` can say "current" or name what changed, and
# `ueia lz4 --build` -- the first step of the pipeline in AGENTS.md -- rebuilds only when it must.
add_custom_command(TARGET ueia_lz4 POST_BUILD
  COMMAND "${CMAKE_COMMAND}"
          -DDLL=$<TARGET_FILE:ueia_lz4>
          -DOUT=${CMAKE_CURRENT_SOURCE_DIR}/bin/ueia_lz4.build.json
          -DSOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}
          -DCOMPILER_ID=${CMAKE_C_COMPILER_ID}
          -DCOMPILER_VERSION=${CMAKE_C_COMPILER_VERSION}
          -DFLAGS=LZ4_DLL_EXPORT=1,LZ4_FAST_DEC_LOOP=1
          -P "${CMAKE_CURRENT_SOURCE_DIR}/tools/stamp_lz4.cmake"
  COMMENT "writing bin/ueia_lz4.build.json" VERBATIM)
