include ../../Makefile.include
DEPS = ../../Makefile.include Makefile

# PS5-only stub: exiv2 references the FreeBSD libprocstat API on any
# __FreeBSD__ target (only in its library-info dump, which Kodi never
# calls); the console has no such library. No upstream source - this
# mirrors the PS5 port's shims/libprocstat/procstat.c one-for-one (see
# scripts/12-build-libuuid-shim.sh in the reference port this fork is
# migrating from).
ifneq ($(OS),ps5)
$(error libprocstat is a PS5-only shim; it has no non-PS5 recipe)
endif

LIBNAME=libprocstat
SHIMDIR=../../../../shims/libprocstat
LIBDYLIB=$(PLATFORM)/lib/$(LIBNAME).a

all: .installed-$(PLATFORM)

.configured-$(PLATFORM): $(DEPS)
	rm -rf $(PLATFORM)/*; mkdir -p $(PLATFORM)
	touch $@

$(LIBDYLIB): .configured-$(PLATFORM)
	mkdir -p $(PLATFORM)/lib
	$(CC) -O2 -fPIC -c $(SHIMDIR)/procstat.c -o $(PLATFORM)/lib/procstat.o
	$(AR) rcs $@ $(PLATFORM)/lib/procstat.o

.installed-$(PLATFORM): $(LIBDYLIB)
	install -d $(PREFIX)/lib
	install -m644 $(LIBDYLIB) $(PREFIX)/lib/libprocstat.a
	touch $@

clean:
	rm -rf $(PLATFORM)
	rm -f .installed-$(PLATFORM) .configured-$(PLATFORM)

distclean::
	rm -rf $(PLATFORM) .installed-$(PLATFORM) .configured-$(PLATFORM)
