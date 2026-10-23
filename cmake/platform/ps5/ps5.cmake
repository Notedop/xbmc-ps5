# PlayStation 5 homebrew platform.
# Graphics: OpenGL 4.6 Core through the ps5-opengl SDK (Mesa/Gallium on AGC),
# presented on a single fullscreen EGL surface. No windowed mode.

set(APP_RENDER_SYSTEM gl)

# The ps5-opengl SDK ships GL and EGL together as one static package with a
# CMake config. Kodi's FindOpenGl/FindEGL modules would otherwise probe the
# sysroot with find_library(GL)/find_library(EGL), which finds pacbrew's
# software osmesa build instead. Both modules start with
#   if(NOT TARGET ${APP_NAME_LC}::<Dep>)
# so pre-creating the targets here makes them skip their probing.
#
# APP_NAME_LC is not defined yet at this point of the configure run (it comes
# from core_find_versions()), so derive it from version.txt the same way.
file(STRINGS "${CMAKE_SOURCE_DIR}/version.txt" _ps5_app_name_line REGEX "^APP_NAME ")
string(REGEX REPLACE "^APP_NAME +" "" _ps5_app_name "${_ps5_app_name_line}")
string(TOLOWER "${_ps5_app_name}" _ps5_app_lc)
if(NOT _ps5_app_lc)
  set(_ps5_app_lc kodi)
endif()

if(NOT PS5_NATIVE_RUNTIME_SDK OR NOT PS5_NATIVE_DEPENDS_MAP)
  message(FATAL_ERROR "PS5 requires PS5_NATIVE_RUNTIME_SDK and PS5_NATIVE_DEPENDS_MAP; use tools/ps5/configure.sh")
endif()
find_program(PS5_NATIVE_PYTHON NAMES python3 REQUIRED NO_CMAKE_FIND_ROOT_PATH)
execute_process(
  COMMAND "${PS5_NATIVE_PYTHON}" "${CMAKE_SOURCE_DIR}/tools/ps5/native_contract.py"
          --sdk "${PS5_NATIVE_RUNTIME_SDK}" --archive-map "${PS5_NATIVE_DEPENDS_MAP}"
  RESULT_VARIABLE _ps5_contract_result ERROR_VARIABLE _ps5_contract_error
  OUTPUT_QUIET)
if(NOT _ps5_contract_result EQUAL 0)
  message(FATAL_ERROR "Invalid native PS5 build inputs: ${_ps5_contract_error}")
endif()
include("${PS5_NATIVE_RUNTIME_SDK}/PS5NativeRuntimeConfig.cmake")
set(_ps5_graphics ${_ps5_app_lc}::OpenGl)
unset(_ps5_contract_result)
unset(_ps5_contract_error)

if(NOT TARGET ${_ps5_app_lc}::OpenGl)
  add_library(${_ps5_app_lc}::OpenGl INTERFACE IMPORTED)
  set_target_properties(${_ps5_app_lc}::OpenGl PROPERTIES
                        INTERFACE_LINK_LIBRARIES ${_ps5_graphics}
                        INTERFACE_COMPILE_DEFINITIONS HAS_GL)
  set(OPENGL_FOUND TRUE CACHE INTERNAL "")
endif()

if(NOT TARGET ${_ps5_app_lc}::EGL)
  add_library(${_ps5_app_lc}::EGL INTERFACE IMPORTED)
  set_target_properties(${_ps5_app_lc}::EGL PROPERTIES
                        INTERFACE_LINK_LIBRARIES ${_ps5_graphics}
                        INTERFACE_COMPILE_DEFINITIONS "HAS_EGL;EGL_NO_X11;MESA_EGL_NO_X11_HEADERS")
  set(EGL_FOUND TRUE CACHE INTERNAL "")
endif()

unset(_ps5_app_name_line)
unset(_ps5_app_name)
unset(_ps5_app_lc)
unset(_ps5_graphics)

set(PLATFORM_REQUIRED_DEPS OpenGl EGL)

# Nothing of this exists on the console.
set(PLATFORM_OPTIONAL_DEPS_EXCLUDE Alsa Avahi Bluetooth CAP CEC DBus LircClient
                                   Pipewire PulseAudio Sndio UDEV)
