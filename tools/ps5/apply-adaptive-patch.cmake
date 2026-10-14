if(NOT DEFINED SOURCE_DIR OR NOT EXISTS "${SOURCE_DIR}/src/common/AdaptiveStream.cpp")
  message(FATAL_ERROR "SOURCE_DIR must identify an extracted inputstream.adaptive source tree")
endif()

set(PATCH_FILE "${CMAKE_CURRENT_LIST_DIR}/patches/inputstream-adaptive-ps5.patch")
set(PATCH_NAME "PS5 adaptive worker shutdown patch")
include("${CMAKE_CURRENT_LIST_DIR}/apply-source-patch.cmake")
