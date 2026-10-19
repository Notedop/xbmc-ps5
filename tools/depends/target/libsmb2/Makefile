include ../../Makefile.include LIBSMB2-VERSION ../../download-files.include
DEPS= ../../Makefile.include Makefile LIBSMB2-VERSION ../../download-files.include \
      001-preserve-reparse-directory-type.patch

# libsmb2 (https://github.com/sahlberg/libsmb2) is the userspace SMB2/3
# client used by the PS5 port's VFS (xbmc/platform/ps5/filesystem/SMB2*).
# Kodi upstream has no native SMB2 client recipe (desktop platforms use
# libsmbclient via FindSmbClient.cmake); this recipe builds the real
# upstream library instead of a shim, matching the reference PS5 port's
# pacbrew PKGBUILD. The local patch preserves directory types for Windows
# reparse points without changing libsmb2's public ABI.

# configuration settings
CMAKE_OPTIONS=-DBUILD_SHARED_LIBS=OFF \
              -DENABLE_EXAMPLES=OFF \
              -DENABLE_LIBKRB5=OFF \
              -DENABLE_GSSAPI=OFF

LIBDYLIB=$(PLATFORM)/build/lib/$(BYPRODUCT)

all: .installed-$(PLATFORM)

.configured-$(PLATFORM): $(DEPS) | $(TARBALLS_LOCATION)/$(ARCHIVE).$(HASH_TYPE)
	-rm -rf $(PLATFORM); mkdir -p $(PLATFORM)
	cd $(PLATFORM); $(ARCHIVE_TOOL) $(ARCHIVE_TOOL_FLAGS) $(TARBALLS_LOCATION)/$(ARCHIVE)
	cd $(PLATFORM); patch -p1 < ../001-preserve-reparse-directory-type.patch
	cd $(PLATFORM); mkdir -p build
	cd $(PLATFORM)/build; $(CMAKE) $(CMAKE_OPTIONS) ..
	touch $@

$(LIBDYLIB): .configured-$(PLATFORM)
	$(MAKE) -C $(PLATFORM)/build

.installed-$(PLATFORM): $(LIBDYLIB)
	$(MAKE) -C $(PLATFORM)/build install
	touch $@

clean:
	$(MAKE) -C $(PLATFORM)/build clean
	rm -f .installed-$(PLATFORM) .configured-$(PLATFORM)

distclean::
	rm -rf $(PLATFORM) .installed-$(PLATFORM) .configured-$(PLATFORM)
