#!/usr/bin/env python3
"""Verify the system-only title, independently of its build command receipt."""
# SPDX-License-Identifier: GPL-3.0-or-later

from native_c_link import nid, require

LOAD_ORDER = ["libkodi-runtime.sprx", "libkodi-gl.sprx", "libkodi-app.sprx"]
SYSTEM = {"libkernel", "libSceLibcInternal"}
STARTUP_SOURCES = {"title_native_launcher.cpp", "kodi_app_entry.cpp", "title_native_startup.h"}
STARTUP_ABI = "ps5-kodi-startup-v1"


def verify(linked, imports):
    require(imports and all(owner["module"] in SYSTEM and owner["library"] == owner["module"]
                            for owner in imports.values()),
            "launcher must have only native kernel/C imports")
    for name in ("sceKernelLoadStartModule", "sceKernelDebugOutText", "_exit"):
        require(imports.get(nid(name)) == {"module": "libkernel", "library": "libkernel"},
                f"launcher requires kernel-owned {name}")
    require(nid("sceKernelDlsym") not in imports,
            "launcher must not perform executable-originated symbol lookup")
    for name in ("snprintf", "atexit", "_init_env", "exit"):
        require(imports.get(nid(name)) ==
                {"module": "libSceLibcInternal", "library": "libSceLibcInternal"},
                f"launcher requires native C {name}")
    symbols = dict(linked.symbols(".symtab"))
    require(all(name in symbols and symbols[name][3] for name in ("_start", "main")),
            "launcher entry points missing")
    forbidden = {"__wrap_malloc", "__wrap_free", "__emutls_get_address", "__cxa_finalize",
                 "__cxa_throw", "__gxx_personality_v0", "__unw_add_dynamic_eh_frame_section",
                 "glClear", "eglMakeCurrent", "ps5_title_runtime_bindings",
                 "ps5_launcher_run_lookup", "ps5_launcher_finalize_lookup"}
    require(not forbidden.intersection(symbols), "launcher contains a shared-runtime dependency "
            "or obsolete lookup marker")
    require(not linked.section(".preinit_array"), "launcher must not require title preinitializers")
    return {"model": "native-two-stage", "native_load_order": LOAD_ORDER,
            "startup_abi": STARTUP_ABI, "entry_handoff": "module-start-arguments",
            "executable_symbol_lookup": False,
            "launcher_has_no_shared_imports": True, "software_relocation": False,
            "application_kept_mapped_until_process_exit": True}
