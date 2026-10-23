# SPDX-License-Identifier: GPL-3.0-or-later
include_guard(GLOBAL)

function(ps5_native_shared_addon target)
  get_target_property(kind ${target} TYPE)
  if(NOT kind STREQUAL "SHARED_LIBRARY")
    message(FATAL_ERROR "Native PS5 add-on ${target} must be an ordinary SHARED library")
  endif()
  set(tools "${PS5_NATIVE_RUNTIME_SDK}/tools")
  target_sources(${target} PRIVATE "${tools}/native_module_crt.cpp")
  set_target_properties(${target} PROPERTIES
    C_COMPILER_LAUNCHER "${PS5_NATIVE_PYTHON};${PS5_NATIVE_COMPILER_LAUNCHER}"
    CXX_COMPILER_LAUNCHER "${PS5_NATIVE_PYTHON};${PS5_NATIVE_COMPILER_LAUNCHER}")
  target_link_options(${target} PRIVATE
    -nodefaultlibs -nostartfiles -nostdlib -nostdlib++
    "LINKER:--as-needed,--no-undefined,--hash-style=sysv,--eh-frame-hdr"
    "LINKER:-z,now,-z,max-page-size=0x4000,-init,module_start,-fini,module_stop"
    "LINKER:-T,${tools}/native-shared.ld")
  target_link_libraries(${target} ${PS5_NATIVE_PROVIDER_LIBRARIES})
  set_property(TARGET ${target} PROPERTY SKIP_BUILD_RPATH TRUE)
  set_property(TARGET ${target} PROPERTY BUILD_WITH_INSTALL_RPATH FALSE)
  set_property(TARGET ${target} APPEND PROPERTY LINK_DEPENDS
    "${tools}/native-shared.ld" ${PS5_NATIVE_PROVIDER_MANIFESTS}
    "${PS5_NATIVE_RUNTIME_SDK}/manifest.json" "${PS5_NATIVE_ADDON_BUILDER}"
    "${tools}/native_shared_link.py" "${tools}/native_runtime_sdk.py"
    "${tools}/native_c_link.py" "${tools}/native_system_provider.py"
    "${PS5_NATIVE_COMPILER_LAUNCHER}"
    "${tools}/ps5-native-tool")
  set(output "${CMAKE_CURRENT_BINARY_DIR}/${target}.sprx")
  add_custom_command(TARGET ${target} POST_BUILD
    COMMAND "${PS5_NATIVE_PYTHON}" "${PS5_NATIVE_ADDON_BUILDER}"
            --sdk "${PS5_NATIVE_RUNTIME_SDK}" --source "$<TARGET_FILE:${target}>"
            --module "${target}" --output "${output}"
    BYPRODUCTS "${output}" "${output}.json"
    VERBATIM COMMENT "Converting and signing native PS5 add-on ${target}")
  set(LIBRARY_LOCATION "${output}" PARENT_SCOPE)
  set(LIBRARY_FILENAME "${target}.sprx" PARENT_SCOPE)
endfunction()
