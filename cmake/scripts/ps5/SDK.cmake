find_program(_PS5_SDK_PYTHON NAMES python3 REQUIRED
             NO_CMAKE_PATH NO_CMAKE_ENVIRONMENT_PATH NO_CMAKE_FIND_ROOT_PATH)
get_filename_component(_PS5_SDK_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
execute_process(
  COMMAND "${_PS5_SDK_PYTHON}" "${_PS5_SDK_ROOT}/tools/ps5/scripts/check-sdk.py"
          --sdk "${PS5_PAYLOAD_SDK}"
  RESULT_VARIABLE _PS5_SDK_RESULT
  OUTPUT_QUIET
  ERROR_VARIABLE _PS5_SDK_ERROR)
if(NOT _PS5_SDK_RESULT EQUAL 0)
  message(FATAL_ERROR "${_PS5_SDK_ERROR}")
endif()
file(GLOB _PS5_SDK_INPUTS
     "${_PS5_SDK_ROOT}/tools/ps5/patches/ps5-payload-sdk/*.patch"
     "${_PS5_SDK_ROOT}/shims/sce_stubs/*.c")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
             "${PS5_PAYLOAD_SDK}/.kodi-sdk.json"
             "${PS5_PAYLOAD_SDK}/bin/prospero-clang"
             "${PS5_PAYLOAD_SDK}/bin/prospero-clang++"
             "${PS5_PAYLOAD_SDK}/bin/ps5-wrapper-args.py"
             "${_PS5_SDK_ROOT}/tools/ps5/scripts/check-sdk.py"
             "${_PS5_SDK_ROOT}/tools/ps5/sdk/PKGBUILD"
             ${_PS5_SDK_INPUTS})
