if(NOT DEFINED SOURCE_DIR OR NOT EXISTS "${SOURCE_DIR}/src/common/AdaptiveStream.cpp")
  message(FATAL_ERROR "SOURCE_DIR must identify an extracted inputstream.adaptive source tree")
endif()

find_program(PATCH_EXECUTABLE patch)
if(NOT PATCH_EXECUTABLE)
  message(FATAL_ERROR "patch is required to apply the PS5 adaptive worker shutdown fix")
endif()
set(patch_file "${CMAKE_CURRENT_LIST_DIR}/patches/inputstream-adaptive-ps5.patch")

execute_process(
  COMMAND "${PATCH_EXECUTABLE}" --batch --force --reverse --dry-run -p1
  WORKING_DIRECTORY "${SOURCE_DIR}"
  INPUT_FILE "${patch_file}"
  RESULT_VARIABLE reverse_result
  OUTPUT_VARIABLE reverse_output
  ERROR_VARIABLE reverse_error)
if(reverse_result EQUAL 0)
  message(STATUS "PS5 adaptive worker shutdown patch already applied")
  return()
endif()

execute_process(
  COMMAND "${PATCH_EXECUTABLE}" --batch --forward --dry-run -p1
  WORKING_DIRECTORY "${SOURCE_DIR}"
  INPUT_FILE "${patch_file}"
  RESULT_VARIABLE check_result
  OUTPUT_VARIABLE check_output
  ERROR_VARIABLE check_error)
if(NOT check_result EQUAL 0)
  message(FATAL_ERROR
    "Adaptive source is incompatible or partially patched; refusing to modify it.\n"
    "${check_output}${check_error}")
endif()

execute_process(
  COMMAND "${PATCH_EXECUTABLE}" --batch --forward -p1
  WORKING_DIRECTORY "${SOURCE_DIR}"
  INPUT_FILE "${patch_file}"
  RESULT_VARIABLE apply_result
  OUTPUT_VARIABLE apply_output
  ERROR_VARIABLE apply_error)
if(NOT apply_result EQUAL 0)
  message(FATAL_ERROR "Failed to apply PS5 adaptive patch:\n${apply_output}${apply_error}")
endif()
message(STATUS "Applied PS5 adaptive worker shutdown patch")
