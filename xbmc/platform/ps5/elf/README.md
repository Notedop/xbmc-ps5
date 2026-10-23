# Experimental native PS5 add-on loader

`CPS5AddonLoader` is a thin `LibraryLoader` adapter over
`sceKernelLoadStartModule`, `sceKernelDlsym` and `sceKernelStopUnloadModule`.
It accepts translated absolute `.sprx` paths only. There is no SO mapper,
host export generator, kernel-table inspection, POSIX loader emulation or fallback.

The normal `DllAddon` export resolution and Kodi function-table lifecycle are
unchanged. Native module start precedes `ADDON_Create`; module stop follows Kodi
instance/add-on destruction and the last shared loader reference. A failed native
release retains the registered loader and quarantines further export access;
it is not retried automatically because a stop may have partially executed.
`GetHModule` is an opaque loader identity, not a public native integer handle.

See [the experiment guide](../../../../docs/PS5-NATIVE-SPRX.md). Historical mapper
tests and tooling in `tools/ps5` apply to the original `Piers`/checkpoint branches,
not this native route.
