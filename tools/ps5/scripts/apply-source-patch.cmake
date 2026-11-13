# Idempotently applies one PS5 patch to an extracted source tree.
# Required: SOURCE_DIR, PATCH_FILE. Optional: PATCH_NAME (for messages).
if(NOT DEFINED SOURCE_DIR OR NOT IS_DIRECTORY "${SOURCE_DIR}")
  message(FATAL_ERROR "SOURCE_DIR must identify an extracted source tree")
endif()
if(NOT DEFINED PATCH_FILE OR NOT EXISTS "${PATCH_FILE}")
  message(FATAL_ERROR "PATCH_FILE must identify an existing patch")
endif()
if(NOT DEFINED PATCH_NAME)
  get_filename_component(PATCH_NAME "${PATCH_FILE}" NAME)
endif()

find_program(PATCH_EXECUTABLE patch)
if(NOT PATCH_EXECUTABLE)
  message(FATAL_ERROR "patch is required to apply ${PATCH_NAME}")
endif()

execute_process(
  COMMAND "${PATCH_EXECUTABLE}" --batch --force --reverse --dry-run --fuzz=0 -p1
  WORKING_DIRECTORY "${SOURCE_DIR}"
  INPUT_FILE "${PATCH_FILE}"
  RESULT_VARIABLE reverse_result
  OUTPUT_VARIABLE reverse_output
  ERROR_VARIABLE reverse_error)
if(reverse_result EQUAL 0)
  message(STATUS "${PATCH_NAME} already applied")
  return()
endif()

execute_process(
  COMMAND "${PATCH_EXECUTABLE}" --batch --forward --dry-run --fuzz=0 -p1
  WORKING_DIRECTORY "${SOURCE_DIR}"
  INPUT_FILE "${PATCH_FILE}"
  RESULT_VARIABLE check_result
  OUTPUT_VARIABLE check_output
  ERROR_VARIABLE check_error)
if(NOT check_result EQUAL 0)
  message(FATAL_ERROR
    "${PATCH_NAME}:\n"
    "Source is incompatible or partially patched; refusing to modify it.\n"
    "${check_output}${check_error}")
endif()

execute_process(
  COMMAND "${PATCH_EXECUTABLE}" --batch --forward --fuzz=0 -p1
  WORKING_DIRECTORY "${SOURCE_DIR}"
  INPUT_FILE "${PATCH_FILE}"
  RESULT_VARIABLE apply_result
  OUTPUT_VARIABLE apply_output
  ERROR_VARIABLE apply_error)
if(NOT apply_result EQUAL 0)
  message(FATAL_ERROR "Failed to apply ${PATCH_NAME}:\n${apply_output}${apply_error}")
endif()
message(STATUS "Applied ${PATCH_NAME}")
