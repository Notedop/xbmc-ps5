include ../../Makefile.include
DEPS = ../../Makefile.include Makefile

# PS5-only shim: getentropy/explicit_bzero/POSIX-at shims libpython needs
# that the SDK's stub libraries lack, plus the ps5_socket() family Python's
# socket module is redirected to (libc_socket.c, from CPython patch
# 24-ps5-socket-libscenet.patch). Linked only at Kodi's final app
# executable link (see cmake/scripts/ps5/ArchSetup.cmake), not by any
# individual dependency's own build. No upstream source - mirrors the PS5
# port's shims/libkodishim/kodishim.c + shims/native-app/libc_socket.c
# one-for-one (see scripts/12-build-libuuid-shim.sh in the reference port
# this fork is migrating from).
ifneq ($(OS),ps5)
$(error libkodishim is a PS5-only shim; it has no non-PS5 recipe)
endif

LIBNAME=libkodishim
SHIMDIR=../../../../shims/libkodishim
NATIVEAPPDIR=../../../../shims/native-app
LIBDYLIB=$(PLATFORM)/lib/$(LIBNAME).a

all: .installed-$(PLATFORM)

.configured-$(PLATFORM): $(DEPS)
	rm -rf $(PLATFORM)/*; mkdir -p $(PLATFORM)
	touch $@

$(LIBDYLIB): .configured-$(PLATFORM)
	mkdir -p $(PLATFORM)/lib
	$(CC) -O2 -fPIC -c $(SHIMDIR)/kodishim.c -o $(PLATFORM)/lib/kodishim.o
	$(CC) -O2 -fPIC -c $(NATIVEAPPDIR)/libc_socket.c -o $(PLATFORM)/lib/libc_socket.o
	$(AR) rcs $@ $(PLATFORM)/lib/kodishim.o $(PLATFORM)/lib/libc_socket.o

.installed-$(PLATFORM): $(LIBDYLIB)
	install -d $(PREFIX)/lib
	install -m644 $(LIBDYLIB) $(PREFIX)/lib/libkodishim.a
	touch $@

clean:
	rm -rf $(PLATFORM)
	rm -f .installed-$(PLATFORM) .configured-$(PLATFORM)

distclean::
	rm -rf $(PLATFORM) .installed-$(PLATFORM) .configured-$(PLATFORM)
