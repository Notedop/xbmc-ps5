set(_PS5_PACKAGE_SCRIPT "${CMAKE_SOURCE_DIR}/tools/ps5/scripts/package.sh")
set(PS5_PACKAGE_STAGE "" CACHE PATH "Reusable PS5 package output (empty selects BUILD/title)")
set(PS5_DEPLOY_HOST "" CACHE STRING "Explicit console FTP host for ps5-deploy")

function(_ps5_package_path name environment fallback description)
  if(DEFINED ENV{${environment}} AND NOT "$ENV{${environment}}" STREQUAL "")
    set(_default "$ENV{${environment}}")
  else()
    set(_default "${fallback}")
  endif()
  set(${name} "${_default}" CACHE PATH "${description}")
endfunction()

_ps5_package_path(PS5_WORK WORK "${CMAKE_SOURCE_DIR}/build/ps5-external" "PS5 external sources")
_ps5_package_path(PS5_OPENGL_SOURCE PS5_OPENGL_SOURCE "${PS5_WORK}/ps5-opengl" "PS5 graphics sources")
_ps5_package_path(PS5_APP_TEMPLATE APP_TEMPLATE
  "${PS5_OPENGL_SOURCE}/build/native-app/PPSA99005" "Built PS5 native-app template")

set(_PS5_PACKAGE_ENV
  "BUILD=${CMAKE_BINARY_DIR}"
  "STAGE=${PS5_PACKAGE_STAGE}"
  "WORK=${PS5_WORK}"
  "APP_TEMPLATE=${PS5_APP_TEMPLATE}"
  "PS5_OPENGL_SOURCE=${PS5_OPENGL_SOURCE}"
  "PS5_PAYLOAD_SDK=${PS5_PAYLOAD_SDK}"
  "PS5_OPENGL_PREFIX=${PS5_OPENGL_PREFIX}"
  "PS5_HOMEBREW_PREFIX=${PS5_HOMEBREW_PREFIX}"
  "PS5_NATIVE_RUNTIME_SDK=${PS5_NATIVE_RUNTIME_SDK}"
  "PS5_NATIVE_DEPENDS_MAP=${PS5_NATIVE_DEPENDS_MAP}"
  "PS5_BUILD_CONFIG=$<CONFIG>")

if(EXISTS "${_PS5_PACKAGE_SCRIPT}")
  add_custom_target(ps5-title
    COMMAND ${CMAKE_COMMAND} -E env
            ${_PS5_PACKAGE_ENV}
            PACKAGE_ZIP=0
            "${_PS5_PACKAGE_SCRIPT}"
    DEPENDS ${APP_NAME_LC}
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    USES_TERMINAL
    VERBATIM
    COMMENT "Building PS5 title folder")

  add_custom_target(ps5-package
    COMMAND ${CMAKE_COMMAND} -E env
            ${_PS5_PACKAGE_ENV}
            PACKAGE_ZIP=1
            "${_PS5_PACKAGE_SCRIPT}"
    DEPENDS ${APP_NAME_LC}
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    USES_TERMINAL
    VERBATIM
    COMMENT "Building PS5 package zip")

  add_custom_target(ps5-deploy
    COMMAND ${CMAKE_COMMAND} -E env ${_PS5_PACKAGE_ENV}
            "${CMAKE_SOURCE_DIR}/tools/ps5/scripts/deploy.sh" "${PS5_DEPLOY_HOST}"
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    USES_TERMINAL
    VERBATIM
    COMMENT "Uploading an existing verified PS5 title (no build or package)")
endif()

unset(_PS5_PACKAGE_SCRIPT)
unset(_PS5_PACKAGE_ENV)
