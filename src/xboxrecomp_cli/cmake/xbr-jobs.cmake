# The CLI's job pool for the generated code. `xbr build` and `package`
# copy this file into the build tree and pass it as
# CMAKE_PROJECT_TOP_LEVEL_INCLUDES (CMake 3.24 or later, Ninja only), with
# XBR_GEN_JOBS (the pool depth) and XBR_GEN_TARGETS (the game.toml
# [build.resources] gen_targets). A gen/ file peaks at about a gigabyte in
# clang, so those targets compile only as many files at once as the free
# memory holds; every other target keeps the full -j. The pool changes the
# order Ninja runs commands in, never a command line: the exe is the same
# at any depth. The game's own CMake is left as it is.
include_guard(GLOBAL)
if(XBR_GEN_JOBS)
  set_property(GLOBAL APPEND PROPERTY JOB_POOLS xbr_gen=${XBR_GEN_JOBS})
  # Deferred to the end of the top-level CMakeLists.txt, where the targets exist.
  cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}" CALL _xbr_gen_pool)
endif()
function(_xbr_gen_pool)
  foreach(t IN LISTS XBR_GEN_TARGETS)
    if(TARGET ${t})
      set_property(TARGET ${t} PROPERTY JOB_POOL_COMPILE xbr_gen)
    else()
      message(WARNING "xbr: no target ${t} for the generated-code pool")
    endif()
  endforeach()
endfunction()
