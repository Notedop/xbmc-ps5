# Kodi cross-compile toolchain for PlayStation 5 homebrew.
#
# Wraps the ps5-payload-sdk toolchain (target x86_64-sie-ps5, FreeBSD-derived
# userland) and points Kodi at:
#   * the pacbrew-built libraries inside the SDK sysroot (/user/homebrew), and
#   * the ps5-opengl SDK (Mesa/Gallium OpenGL 4.6 Core + EGL) for graphics.
#
# Usage (from an empty build dir):
#   cmake -DCMAKE_TOOLCHAIN_FILE=/path/to/ps5-kodi.cmake [options] /path/to/kodi
#
# Environment overrides: PS5_PAYLOAD_SDK, PS5_OPENGL_PREFIX, PS5_NATIVE_RUNTIME_SDK,
# PS5_DEPENDS_PREFIX, PS5_HOMEBREW_PREFIX

if(NOT DEFINED PS5_PAYLOAD_SDK)
  if(DEFINED ENV{PS5_PAYLOAD_SDK})
    set(PS5_PAYLOAD_SDK "$ENV{PS5_PAYLOAD_SDK}")
  else()
    set(PS5_PAYLOAD_SDK "/opt/ps5-payload-sdk")
  endif()
endif()
set(PS5_PAYLOAD_SDK "${PS5_PAYLOAD_SDK}" CACHE PATH "ps5-payload-sdk install directory")

if(NOT EXISTS "${PS5_PAYLOAD_SDK}/toolchain/prospero.cmake")
  message(FATAL_ERROR "ps5-payload-sdk not found at ${PS5_PAYLOAD_SDK} (set PS5_PAYLOAD_SDK)")
endif()

# John Tornblom's toolchain: sets CMAKE_SYSTEM_NAME FreeBSD, the sysroot,
# prospero-clang/++ wrappers, static-only linking and PIE.
include("${PS5_PAYLOAD_SDK}/toolchain/prospero.cmake")

# The SDK toolchain prefers a library's own CMake package over Find modules.
# Kodi's Find modules must run (they create the kodi::<Dep> targets the build
# links against), so restore CMake's default module-first search here.
set(CMAKE_FIND_PACKAGE_PREFER_CONFIG FALSE)

# --- Kodi platform selection -------------------------------------------------
set(CORE_SYSTEM_NAME ps5 CACHE STRING "Kodi core system name" FORCE)
set(CORE_PLATFORM_NAME ps5 CACHE STRING "Kodi platform" FORCE)
set(APP_RENDER_SYSTEM gl CACHE STRING "Kodi render system" FORCE)

# NOTE: nothing in this file may force CMAKE_INSTALL_PREFIX, CMAKE_CXX_STANDARD
# or CMAKE_PREFIX_PATH: Kodi passes this same toolchain file to the CMake
# sub-builds of its internal libraries, which install into the build tree and
# get their own -D arguments.

# CMake 3.28+ scans C++20 targets for modules with clang-scan-deps, which the
# SDK's compiler wrapper does not provide. Kodi does not use modules.
set(CMAKE_CXX_SCAN_FOR_MODULES OFF)

# --- Libraries built from source via tools/depends/target -------------------
# FFmpeg, dav1d and CPython (plus CPython's own transitive deps: expat, libffi,
# libintl, libiconv, liblzma) are built against this toolchain by
# tools/depends/target/{ffmpeg,dav1d,python3,...} (see docs/README.PS5.md ss1)
# instead of taken from the SDK's pacbrew sysroot. That prefix lives outside
# PS5_PAYLOAD_SDK, so it must be added to CMAKE_FIND_ROOT_PATH explicitly or
# find_package(Python3)/find_library() (root-path-restricted while cross
# compiling) would never see it. It must also come BEFORE the pacbrew root
# below: find_library()/find_package() take the first match across
# CMAKE_FIND_ROOT_PATH entries in order, and several of these libraries
# (expat, libffi, libintl, ...) exist in both roots - without this ordering
# the pacbrew copies would silently keep winning. configure.sh's
# kodi-pkg-config wrapper does the pkg-config-based equivalent of this
# ordering for FFmpeg/dav1d (FindFFMPEG.cmake uses pkg_check_modules, not
# find_library/CMAKE_FIND_ROOT_PATH, for those).
if(NOT DEFINED PS5_DEPENDS_PREFIX)
  if(DEFINED ENV{PS5_DEPENDS_PREFIX})
    set(PS5_DEPENDS_PREFIX "$ENV{PS5_DEPENDS_PREFIX}")
  endif()
endif()
if(PS5_DEPENDS_PREFIX)
  set(PS5_DEPENDS_PREFIX "${PS5_DEPENDS_PREFIX}" CACHE PATH "tools/depends/target install prefix (ffmpeg/dav1d/python3)")
  list(APPEND CMAKE_FIND_ROOT_PATH "${PS5_DEPENDS_PREFIX}")
endif()

# --- Libraries built by pacbrew-repo (inside the sysroot) -------------------
if(NOT DEFINED PS5_HOMEBREW_PREFIX)
  if(DEFINED ENV{PS5_HOMEBREW_PREFIX})
    set(PS5_HOMEBREW_PREFIX "$ENV{PS5_HOMEBREW_PREFIX}")
  else()
    set(PS5_HOMEBREW_PREFIX "${PS5_PAYLOAD_SDK}/target/user/homebrew")
  endif()
endif()
set(PS5_HOMEBREW_PREFIX "${PS5_HOMEBREW_PREFIX}" CACHE PATH "PS5 homebrew dependency prefix")
set(PS5_HBROOT "${PS5_HOMEBREW_PREFIX}")
list(APPEND CMAKE_FIND_ROOT_PATH "${PS5_HBROOT}")
set(ENV{PKG_CONFIG_LIBDIR} "${PS5_HBROOT}/lib/pkgconfig:${PS5_PAYLOAD_SDK}/target/lib/pkgconfig")
set(ENV{PKG_CONFIG_SYSROOT_DIR} "${PS5_PAYLOAD_SDK}/target")

# --- ps5-opengl SDK ----------------------------------------------------------
if(NOT DEFINED PS5_OPENGL_PREFIX)
  if(DEFINED ENV{PS5_OPENGL_PREFIX})
    set(PS5_OPENGL_PREFIX "$ENV{PS5_OPENGL_PREFIX}")
  else()
    set(PS5_OPENGL_PREFIX "/opt/ps5-opengl-gl46")
  endif()
endif()
set(PS5_OPENGL_PREFIX "${PS5_OPENGL_PREFIX}" CACHE PATH "ps5-opengl relocatable SDK prefix")
set(PS5OpenGL_DIR "${PS5_OPENGL_PREFIX}/lib/cmake/PS5OpenGL" CACHE PATH "PS5OpenGL CMake config dir")
list(APPEND CMAKE_FIND_ROOT_PATH "${PS5_OPENGL_PREFIX}")
set(ENV{PKG_CONFIG_LIBDIR} "$ENV{PKG_CONFIG_LIBDIR}:${PS5_OPENGL_PREFIX}/lib/pkgconfig")

if(NOT DEFINED PS5_NATIVE_RUNTIME_SDK)
  set(PS5_NATIVE_RUNTIME_SDK "$ENV{PS5_NATIVE_RUNTIME_SDK}")
endif()
set(PS5_NATIVE_RUNTIME_SDK "${PS5_NATIVE_RUNTIME_SDK}" CACHE PATH
    "Required prepared native shared runtime SDK")
if(NOT DEFINED PS5_NATIVE_DEPENDS_MAP)
  set(PS5_NATIVE_DEPENDS_MAP "$ENV{PS5_NATIVE_DEPENDS_MAP}")
endif()
set(PS5_NATIVE_DEPENDS_MAP "${PS5_NATIVE_DEPENDS_MAP}" CACHE FILEPATH
    "Verified isolated native static dependency replacements")
if(NOT PS5_NATIVE_RUNTIME_SDK)
  message(FATAL_ERROR "PS5 requires a prepared PS5_NATIVE_RUNTIME_SDK; use tools/ps5/configure.sh. The static runtime profile is no longer supported.")
endif()
if(PS5_NATIVE_RUNTIME_SDK)
  foreach(_file include/stdio.h tools/native-clang.py PS5NativeRuntimeConfig.cmake)
    if(NOT EXISTS "${PS5_NATIVE_RUNTIME_SDK}/${_file}")
      message(FATAL_ERROR "Incomplete PS5 native runtime SDK: ${PS5_NATIVE_RUNTIME_SDK}/${_file}")
    endif()
  endforeach()
  find_program(PS5_NATIVE_PYTHON NAMES python3 REQUIRED NO_CMAKE_FIND_ROOT_PATH)
  set(_ps5_launcher "${PS5_NATIVE_PYTHON};${PS5_NATIVE_RUNTIME_SDK}/tools/native-clang.py")
  set(CMAKE_C_COMPILER_LAUNCHER "${_ps5_launcher}")
  set(CMAKE_CXX_COMPILER_LAUNCHER "${_ps5_launcher}")
  foreach(_language C CXX)
    string(FIND "${CMAKE_${_language}_FLAGS_INIT}"
           "-I\"${PS5_NATIVE_RUNTIME_SDK}/include\"" _ps5_overlay_position)
    if(_ps5_overlay_position EQUAL -1)
      if(_language STREQUAL "CXX")
        string(APPEND CMAKE_CXX_FLAGS_INIT
               " -nostdinc++ -I\"${PS5_PAYLOAD_SDK}/target/include/c++/v1\"")
      endif()
      string(APPEND CMAKE_${_language}_FLAGS_INIT
             " -fPIC -femulated-tls -fno-stack-protector -I\"${PS5_NATIVE_RUNTIME_SDK}/include\"")
    endif()
  endforeach()
  list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES
       PS5_NATIVE_RUNTIME_SDK PS5_DEPENDS_PREFIX PS5_HOMEBREW_PREFIX)
  unset(_ps5_launcher)
  unset(_ps5_overlay_position)
  unset(_language)
  unset(_file)
endif()
