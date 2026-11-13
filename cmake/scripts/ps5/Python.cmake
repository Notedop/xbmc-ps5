# Target development artifacts only; Python interpreters, SWIG and Java are host tools.
if(NOT ENABLE_PYTHON)
  return()
endif()
if(CMAKE_VERSION VERSION_LESS 3.22)
  message(FATAL_ERROR "PS5 target Python discovery requires CMake 3.22 or newer")
endif()

set(PS5_PYTHON_VERSION "3.14" CACHE STRING "PS5 target CPython major.minor version")
if(NOT PS5_PYTHON_VERSION MATCHES "^3\\.[0-9]+$")
  message(FATAL_ERROR "PS5_PYTHON_VERSION must be a Python 3 major.minor version")
endif()
if(NOT IS_ABSOLUTE "${PS5_DEPENDS_PREFIX}")
  message(FATAL_ERROR "PS5 Python requires an absolute PS5_DEPENDS_PREFIX")
endif()
if(PYTHON_PATH AND NOT PYTHON_PATH STREQUAL PS5_DEPENDS_PREFIX)
  message(FATAL_ERROR "PS5 Python uses PS5_DEPENDS_PREFIX; remove conflicting PYTHON_PATH")
endif()
if(PYTHON_VER AND NOT PYTHON_VER STREQUAL PS5_PYTHON_VERSION)
  message(FATAL_ERROR "PS5 Python uses PS5_PYTHON_VERSION; remove conflicting PYTHON_VER")
endif()

# FindPython removes the legacy cache hints. Restore them on every toolchain
# load, including plain CMake regeneration, and replace artifacts when the prefix changes.
set(PYTHON_PATH "${PS5_DEPENDS_PREFIX}")
set(PYTHON_VER "${PS5_PYTHON_VERSION}")
set(Python3_ROOT_DIR "${PS5_DEPENDS_PREFIX}" CACHE PATH "PS5 target Python prefix" FORCE)
set(Python3_USE_STATIC_LIBS TRUE CACHE BOOL "Use PS5 target Python static library" FORCE)
# FindPython3 can probe python-config even with explicit development artifacts.
# Neither a host configuration script nor a target executable describes this pair.
unset(_Python3_CONFIG CACHE)
set(_Python3_CONFIG "")
set(Python3_INCLUDE_DIR "${PS5_DEPENDS_PREFIX}/include/python${PS5_PYTHON_VERSION}"
    CACHE PATH "PS5 target Python headers" FORCE)
set(Python3_LIBRARY "${PS5_DEPENDS_PREFIX}/lib/libpython${PS5_PYTHON_VERSION}.a"
    CACHE FILEPATH "PS5 target Python static library" FORCE)
foreach(_ps5_python_file
        "${Python3_INCLUDE_DIR}/Python.h"
        "${Python3_INCLUDE_DIR}/patchlevel.h"
        "${Python3_INCLUDE_DIR}/pyconfig.h"
        "${Python3_LIBRARY}")
  if(NOT EXISTS "${_ps5_python_file}")
    message(FATAL_ERROR "Incomplete PS5 target Python: ${_ps5_python_file}")
  endif()
endforeach()
unset(_ps5_python_file)

# Kodi's Python module otherwise treats failed Development discovery as optional.
set(CMAKE_REQUIRE_FIND_PACKAGE_Python3 TRUE)
