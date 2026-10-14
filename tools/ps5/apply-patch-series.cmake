# Applies every NNNN-name.patch in SERIES_DIR, in order, to SOURCE_DIR.
# Required: SOURCE_DIR, SERIES_DIR. Optional: SERIES_NAME (for messages).
#
# Applied patches are recorded with their SHA-256 in
# SOURCE_DIR/.ps5-applied-patches so a re-run skips them. A recorded patch
# that has since changed or left the series cannot be undone safely, so that
# is an error asking for a fresh source tree.
cmake_minimum_required(VERSION 3.13)

if(NOT DEFINED SOURCE_DIR OR NOT IS_DIRECTORY "${SOURCE_DIR}")
  message(FATAL_ERROR "SOURCE_DIR must identify an extracted source tree")
endif()
if(NOT DEFINED SERIES_DIR OR NOT IS_DIRECTORY "${SERIES_DIR}")
  message(FATAL_ERROR "SERIES_DIR must identify a patch series directory")
endif()
string(REGEX REPLACE "/+$" "" SERIES_DIR "${SERIES_DIR}")
if(NOT DEFINED SERIES_NAME)
  get_filename_component(SERIES_NAME "${SERIES_DIR}" NAME)
endif()

set(stamp_file "${SOURCE_DIR}/.ps5-applied-patches")
set(stamped_names "")
set(stamped_hashes "")
if(EXISTS "${stamp_file}")
  file(STRINGS "${stamp_file}" stamp_lines)
  foreach(line IN LISTS stamp_lines)
    if(line MATCHES "^([^ ]+) ([0-9a-f]+)$")
      list(APPEND stamped_names "${CMAKE_MATCH_1}")
      list(APPEND stamped_hashes "${CMAKE_MATCH_2}")
    endif()
  endforeach()
endif()

file(GLOB entries RELATIVE "${SERIES_DIR}" "${SERIES_DIR}/*")
set(patches "")
foreach(entry IN LISTS entries)
  if(IS_DIRECTORY "${SERIES_DIR}/${entry}" OR NOT entry MATCHES "\\.patch$")
    continue()
  endif()
  if(NOT entry MATCHES "^[0-9][0-9][0-9][0-9]-[a-z0-9-]+\\.patch$")
    message(FATAL_ERROR "${SERIES_NAME}: ${entry} does not match NNNN-name.patch")
  endif()
  list(APPEND patches "${entry}")
endforeach()
list(SORT patches)

set(refresh_hint "Delete ${SOURCE_DIR} and reconfigure to re-extract a clean source tree.")
foreach(name IN LISTS stamped_names)
  if(NOT name IN_LIST patches)
    message(FATAL_ERROR "${SERIES_NAME}: ${name} was applied but is no longer in the series. ${refresh_hint}")
  endif()
endforeach()

foreach(name IN LISTS patches)
  file(SHA256 "${SERIES_DIR}/${name}" hash)
  list(FIND stamped_names "${name}" index)
  if(NOT index EQUAL -1)
    list(GET stamped_hashes ${index} stamped_hash)
    if(stamped_hash STREQUAL hash)
      message(STATUS "${SERIES_NAME}/${name} already applied")
      continue()
    endif()
    message(FATAL_ERROR "${SERIES_NAME}: ${name} changed since it was applied. ${refresh_hint}")
  endif()

  execute_process(
    COMMAND "${CMAKE_COMMAND}" "-DSOURCE_DIR=${SOURCE_DIR}"
            "-DPATCH_FILE=${SERIES_DIR}/${name}"
            "-DPATCH_NAME=${SERIES_NAME}/${name}"
            -P "${CMAKE_CURRENT_LIST_DIR}/apply-source-patch.cmake"
    RESULT_VARIABLE apply_result)
  if(NOT apply_result EQUAL 0)
    message(FATAL_ERROR "${SERIES_NAME}: failed to apply ${name}")
  endif()
  file(APPEND "${stamp_file}" "${name} ${hash}\n")
endforeach()
