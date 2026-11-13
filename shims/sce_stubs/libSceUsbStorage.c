/* SPDX-License-Identifier: GPL-2.0-or-later */
#define STUB(name) asm(".global " #name "\n.type " #name " @function\n" #name ":\n")

STUB(_ZN23sceAutoMounterIpcClient14requestUnmountEPKc);
STUB(sceUsbStorageCheckSystemDefaultMountPoint);
STUB(sceUsbStorageClearFakeMapLockForDebug);
STUB(sceUsbStorageClearFakeWriteProtection);
STUB(sceUsbStorageGetDeviceInfo);
STUB(sceUsbStorageGetDeviceList);
STUB(sceUsbStorageGetMountPointOfShellCore);
STUB(sceUsbStorageGetdentsClose);
STUB(sceUsbStorageGetdentsOpen);
STUB(sceUsbStorageGetdentsRead);
STUB(sceUsbStorageInit);
STUB(sceUsbStorageIsExist);
STUB(sceUsbStorageRegisterCallback);
STUB(sceUsbStorageRegisterCallbackForMapAvailable);
STUB(sceUsbStorageRegisterCallbackForMapUnavailable);
STUB(sceUsbStorageRequestMap);
STUB(sceUsbStorageRequestMapWSB);
STUB(sceUsbStorageRequestUnmap);
STUB(sceUsbStorageSetFakeMapLockForDebug);
STUB(sceUsbStorageSetFakeWriteProtection);
STUB(sceUsbStorageTerm);
STUB(sceUsbStorageUnregisterCallback);
STUB(sceUsbStorageUnregisterCallbackForMapAvailable);
STUB(sceUsbStorageUnregisterCallbackForMapUnavailable);
