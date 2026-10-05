# FindPython
# --------
# Finds Python3 libraries
#
# This module will search for the required python libraries on the system
# If multiple versions are found, the highest version will be used.
#
# --------
#
# the following variables influence behaviour:
#
# PYTHON_PATH - use external python not found in system paths
#               usage: -DPYTHON_PATH=/path/to/python/lib
# PYTHON_VER - use exact python version, fail if not found
#               usage: -DPYTHON_VER=3.8
#
# --------
#
# This will define the following targets:
#
#   ${APP_NAME_LC}::Python - The Python library

if(NOT TARGET ${APP_NAME_LC}::${CMAKE_FIND_PACKAGE_NAME})
  # for Depends/Windows builds, set search root dir to libdir path
  if(KODI_DEPENDSBUILD
     OR CMAKE_SYSTEM_NAME STREQUAL WINDOWS
     OR CMAKE_SYSTEM_NAME STREQUAL WindowsStore)
    set(Python3_USE_STATIC_LIBS TRUE)
    set(Python3_ROOT_DIR ${libdir})
  endif()

  # Provide root dir to search for Python if provided
  if(PYTHON_PATH)
    set(Python3_ROOT_DIR ${PYTHON_PATH})

    # unset cache var so we can generate again with a different dir (or none) if desired
    unset(PYTHON_PATH CACHE)
  endif()

  # Set specific version of Python to find if provided
  if(PYTHON_VER)
    set(VERSION ${PYTHON_VER})
    set(EXACT_VER "EXACT")

    # unset cache var so we can generate again with a different ver (or none) if desired
    unset(PYTHON_VER CACHE)
  endif()

  find_package(Python3 ${VERSION} ${EXACT_VER} COMPONENTS Development ${SEARCH_QUIET})

  if(Python3_FOUND)
    # PS5's CPython is built from source via tools/depends/target/python3
    # (see docs/README.PS5.md ss1), the same way a KODI_DEPENDSBUILD target
    # is - so it needs the same transitive static libs explicitly linked in,
    # even though PS5 isn't a KODI_DEPENDSBUILD platform. _decimal/_ctypes
    # are disabled in that recipe (tools/depends/target/python3/Makefile),
    # so gmp/ffi are not actually required there; keep them optional instead
    # of REQUIRED for ps5 so this doesn't fail if they are ever absent.
    # expat IS linked the same way as KODI_DEPENDSBUILD (tools/depends'
    # expat, 2.8.1) - not the SDK pacbrew sysroot's older bundled expat
    # (2.6.2) that fontconfig/freetype/harfbuzz still pull in (ss0.3): the
    # kodi-pkg-config wrapper in tools/ps5/configure.sh routes fontconfig's
    # "Requires.private: expat" to this same tools/depends expat too, so the
    # whole static link only ever sees one expat.a, not two conflicting ones.
    if(CORE_SYSTEM_NAME STREQUAL ps5)
      set(PS5_PYTHON_DEPENDSBUILD TRUE)
    endif()

    if(KODI_DEPENDSBUILD OR PS5_PYTHON_DEPENDSBUILD)
      set(EXPAT_USE_STATIC_LIBS TRUE)
      find_package(EXPAT REQUIRED ${SEARCH_QUIET})

      if(PS5_PYTHON_DEPENDSBUILD)
        find_library(FFI_LIBRARY ffi)
        find_library(GMP_LIBRARY gmp)
      else()
        find_library(FFI_LIBRARY ffi REQUIRED)
        find_library(GMP_LIBRARY gmp REQUIRED)
      endif()

      find_package(Iconv REQUIRED ${SEARCH_QUIET})
      find_package(Intl REQUIRED ${SEARCH_QUIET})
      find_package(LibLZMA REQUIRED ${SEARCH_QUIET})

      if(NOT CORE_SYSTEM_NAME STREQUAL android)
        if(CORE_SYSTEM_NAME STREQUAL wasm OR CORE_SYSTEM_NAME STREQUAL ps5)
          # Emscripten and PS5 do not provide native libdl/libutil - the
          # recipe's fork/exec/forkpty-dependent modules (_posixsubprocess,
          # _multiprocessing, _ctypes) are disabled for both platforms, so
          # neither library is actually needed at link time.
          set(PYTHON_DEP_LIBRARIES pthread)
        else()
          set(PYTHON_DEP_LIBRARIES pthread dl util)
        endif()
        if(CORE_SYSTEM_NAME STREQUAL linux)
          # python archive built via depends requires librt for _posixshmem library
          list(APPEND PYTHON_DEP_LIBRARIES rt)
        endif()
      endif()

      set(Py_LINK_LIBRARIES LIBRARY::Iconv Intl::Intl LibLZMA::LibLZMA EXPAT::EXPAT ${PYTHON_DEP_LIBRARIES})
      if(FFI_LIBRARY)
        list(APPEND Py_LINK_LIBRARIES ${FFI_LIBRARY})
      endif()
      if(GMP_LIBRARY)
        list(APPEND Py_LINK_LIBRARIES ${GMP_LIBRARY})
      endif()
    endif()

    # We use this all over the place. Maybe it would be nice to keep it as a TARGET property
    # but for now a cached variable will do
    set(PYTHON_VERSION "${Python3_VERSION_MAJOR}.${Python3_VERSION_MINOR}" CACHE INTERNAL "" FORCE)

    add_library(${APP_NAME_LC}::${CMAKE_FIND_PACKAGE_NAME} UNKNOWN IMPORTED)
    set_target_properties(${APP_NAME_LC}::${CMAKE_FIND_PACKAGE_NAME} PROPERTIES
                                                                     IMPORTED_LOCATION "${Python3_LIBRARIES}"
                                                                     INTERFACE_INCLUDE_DIRECTORIES "${Python3_INCLUDE_DIRS}"
                                                                     INTERFACE_LINK_OPTIONS "${Python3_LINK_OPTIONS}"
                                                                     INTERFACE_COMPILE_DEFINITIONS HAS_PYTHON)

    if(Py_LINK_LIBRARIES)
      set_target_properties(${APP_NAME_LC}::${CMAKE_FIND_PACKAGE_NAME} PROPERTIES
                                                                       INTERFACE_LINK_LIBRARIES "${Py_LINK_LIBRARIES}")
    endif()
  endif()
endif()
