# SPDX-License-Identifier: GPL-3.0-or-later
include_guard(GLOBAL)

# Restricted opt-in path: one C object, no imports, with the Kodi C entrypoints.
function(add_ps5_native_c_addon target)
  cmake_parse_arguments(PARSE_ARGV 1 arg "" "SOURCE;MANIFEST;SDK;NATIVE_TOOL;OUTPUT_DIRECTORY"
                       "INCLUDE_DIRECTORIES;COMPILE_DEFINITIONS")
  if(arg_UNPARSED_ARGUMENTS OR arg_KEYWORDS_MISSING_VALUES)
    message(FATAL_ERROR "Invalid native C add-on arguments: ${arg_UNPARSED_ARGUMENTS};${arg_KEYWORDS_MISSING_VALUES}")
  endif()
  foreach(required SOURCE MANIFEST SDK NATIVE_TOOL OUTPUT_DIRECTORY)
    if(NOT arg_${required})
      message(FATAL_ERROR "add_ps5_native_c_addon requires ${required}")
    endif()
  endforeach()
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  get_filename_component(root "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../../.." ABSOLUTE)
  foreach(path SOURCE MANIFEST)
    get_filename_component(arg_${path} "${arg_${path}}" ABSOLUTE BASE_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
  endforeach()
  get_filename_component(output "${arg_OUTPUT_DIRECTORY}" ABSOLUTE BASE_DIR "${CMAKE_CURRENT_BINARY_DIR}")
  set(extra_args)
  foreach(path IN LISTS arg_INCLUDE_DIRECTORIES)
    get_filename_component(path "${path}" ABSOLUTE BASE_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
    list(APPEND extra_args --include "${path}")
  endforeach()
  foreach(define IN LISTS arg_COMPILE_DEFINITIONS)
    list(APPEND extra_args "--define=${define}")
  endforeach()
  add_custom_target(${target} ALL
    COMMAND "${Python3_EXECUTABLE}" "${root}/tools/ps5/build-native-c-addon.py"
            --source "${arg_SOURCE}" --manifest "${arg_MANIFEST}" --build "${output}"
            --sdk "${arg_SDK}" --native-tool "${arg_NATIVE_TOOL}" ${extra_args}
    VERBATIM
    COMMENT "Building restricted PS5 native C add-on ${target}")
endfunction()
