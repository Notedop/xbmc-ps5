set(_PS5_PACKAGE_SCRIPT "${CMAKE_SOURCE_DIR}/tools/ps5/package.sh")

if(EXISTS "${_PS5_PACKAGE_SCRIPT}")
  add_custom_target(ps5-title
    COMMAND ${CMAKE_COMMAND} -E env
            BUILD=${CMAKE_BINARY_DIR}
            STAGE=${CMAKE_SOURCE_DIR}/build/ps5-stage
            "${_PS5_PACKAGE_SCRIPT}"
    DEPENDS ${APP_NAME_LC}
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    USES_TERMINAL
    COMMENT "Building PS5 title folder")

  add_custom_target(ps5-package
    COMMAND ${CMAKE_COMMAND} -E env
            BUILD=${CMAKE_BINARY_DIR}
            STAGE=${CMAKE_SOURCE_DIR}/build/ps5-stage
            PACKAGE_ZIP=1
            "${_PS5_PACKAGE_SCRIPT}"
    DEPENDS ${APP_NAME_LC}
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    USES_TERMINAL
    COMMENT "Building PS5 package zip")
endif()

unset(_PS5_PACKAGE_SCRIPT)
