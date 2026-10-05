include ../../Makefile.include PYTHON3-VERSION ../../download-files.include
DEPS = ../../Makefile.include Makefile PYTHON3-VERSION ../../download-files.include \
                                 apple.patch \
                                 process-vm-ready.patch \
                                 static-compile.patch \
                                 20-ps5-freebsd-cross-host.patch \
                                 21-ps5-no-subprocess.patch \
                                 24-ps5-socket-libscenet.patch \
                                 25-ps5-stdio-nomacros.patch \
                                 ps5_compat.h \
                                 ps5_pysocket.h \
                                 10-android-modules.patch \
                                 11-android-ctypes.patch \
                                 13-wasm-em-config-clang.patch \
                                 14-wasm-build-wasm-default.patch \
                                 15-wasm-static-hacl.patch \
                                 16-ios-static-nonframework.patch \
                                 17-ios-simulator-version-min.patch \
                                 18-ios-objc-fallback.patch \
                                 19-tvos-disable-fork-exec.patch

ifeq ($(findstring apple-darwin, $(HOST)), apple-darwin)
  ifeq ($(OS), darwin_embedded)
    EXTRA_CONFIGURE=ac_cv_func_wait3=no ac_cv_func_wait4=no ac_cv_func_waitpid=no \
                    ac_cv_func_execv=no ac_cv_func_fexecv=no ac_cv_func_getentropy=no \
                    ac_cv_func_setpriority=no ac_cv_func_sendfile=no ac_cv_header_sched_h=no \
                    ac_cv_func_posix_spawn=no ac_cv_func_posix_spawnp=no \
                    ac_cv_func_forkpty=no ac_cv_lib_util_forkpty=no \
                    ac_cv_func_getgroups=no \
                    ac_cv_func_system=no \
                    ac_cv_func_dup3=no \
                    ac_cv_func_pipe2=no
    export SDKROOT
  endif
  ifeq ($(OS), osx)
    export SDKROOT
  endif
  # required for _localemodule
  EXTRA_CONFIGURE+= ac_cv_lib_intl_textdomain=yes

  # Python already handles older OSs correctly internally
  # config.site appends a CFLAGS given here after the platform's, so it overrides them
  EXTRA_CONFIGURE+= CFLAGS=-Wno-error=unguarded-availability-new
endif

ifeq ($(OS), linux)
  EXTRA_CONFIGURE=ac_cv_pthread=yes
  ifeq ($(TARGET_PLATFORM),webos)
    # Force intl check to succeed
    EXTRA_CONFIGURE+=ac_cv_lib_intl_textdomain=yes
  endif
endif

ifeq ($(OS),wasm)
  # Cross-compiling to wasm cannot run AX_C_FLOAT_WORDS_BIGENDIAN's probe.
  # WebAssembly is little-endian, so preset the cache result. Also disable
  # forkpty: posixmodule builds the forkpty wrapper under HAVE_FORKPTY, but the
  # PyOS_*Fork helpers it calls are only built when HAVE_FORK is available.
  # memfd_create is also misdetected here and fails at link time with emcc.
  EXTRA_CONFIGURE+= ax_cv_c_float_words_bigendian=no \
                    ac_cv_func_forkpty=no ac_cv_lib_util_forkpty=no \
                    ac_cv_func_memfd_create=no
endif

# Disabled c extension modules for all platforms
PY_MODULES = py_cv_module_grp=n/a \
             py_cv_module_syslog=n/a \
             py_cv_module__dbm=n/a \
             py_cv_module__gdbm=n/a \
             py_cv_module__uuid=n/a \
             py_cv_module_readline=n/a \
             py_cv_module__curses=n/a \
             py_cv_module__curses_panel=n/a \
             py_cv_module_xx=n/a \
             py_cv_module_xxlimited=n/a \
             py_cv_module_xxlimited_35=n/a \
             py_cv_module_xxsubtype=n/a \
             py_cv_module__xxsubinterpreters=n/a \
             py_cv_module__tkinter=n/a \
             py_cv_module__curses=n/a \
             py_cv_module__codecs_jp=n/a \
             py_cv_module__codecs_kr=n/a \
             py_cv_module__codecs_tw=n/a

# These modules use "internal" libs for building. The required static archives
# are not installed outside of the cpython build tree, and cause failure in kodi linking
# If we wish to support them in the future, we should create "system libs" for them
PY_MODULES+= py_cv_module__decimal=n/a \
             py_cv_module__sha2=n/a \
             py_cv_module__zstd=n/a


ifeq ($(OS), darwin_embedded)
  PY_MODULES+= py_cv_module__posixsubprocess=n/a \
               py_cv_module__scproxy=n/a

  # A real iOS triplet, not the tree's darwin one: CPython derives ac_sys_system,
  # the deployment target and the simulator split from it.
  ifeq ($(findstring iphone,$(TARGET_PLATFORM)),iphone)
    PYTHON_HOST=$(subst -darwin,-ios$(APPLE_DEPLOYMENT_TARGET),$(HOST))
    ifeq ($(TARGET_PLATFORM),iphonesimulator)
      PYTHON_HOST:=$(PYTHON_HOST)-simulator
    endif
    EXTRA_CONFIGURE+= --host=$(PYTHON_HOST)
  endif
endif

ifeq ($(OS),ps5)
  # Align with known-working PS5 pacbrew python3 profile:
  # no subprocess/process-control modules in title runtime and conservative
  # cache overrides for missing/undocumented SDK symbols.
  PY_MODULES+= py_cv_module__posixsubprocess=n/a \
               py_cv_module__posixshmem=n/a \
               py_cv_module__multiprocessing=n/a \
               py_cv_module__ctypes=n/a
  # pyexpat links against an external expat (default --with-system-expat=yes
  # above), resolved via tools/depends/target/expat (2.8.1) rather than the
  # SDK pacbrew sysroot's older bundled expat (2.6.2) - the latter is missing
  # newer expat API this CPython version's pyexpat.c calls, and PS5 still
  # sources fontconfig/freetype/harfbuzz from that SDK sysroot (see
  # docs/README.PS5.md ss0.3), which pulls that same older expat in
  # transitively. Having both expat.a builds present in one static
  # whole-archive link produces duplicate-symbol errors, not just a version
  # mismatch, so tools/ps5/configure.sh's kodi-pkg-config wrapper routes
  # fontconfig's "Requires.private: expat" to this same depends-built expat
  # too, keeping exactly one expat.a in the final link.
  # ac_cv_have_getc_unlocked (not ac_cv_func_getc_unlocked, which this
  # configure.ac does not define) forces off the "getc_unlocked() and
  # friends" probe in configure.ac: the SDK's FreeBSD <stdio.h> declares
  # these, so the link-only probe succeeds even though the title's
  # clean-room libc.prx does not share FreeBSD's FILE layout. See
  # 25-ps5-stdio-nomacros.patch for the matching Python.h macro removal.
  EXTRA_CONFIGURE+= ac_cv_func_wait=no ac_cv_func_wait3=no ac_cv_func_wait4=no ac_cv_func_waitpid=no \
                    ac_cv_func_execv=no ac_cv_func_fexecve=no ac_cv_func_setpriority=no ac_cv_func_sendfile=no \
                    ac_cv_header_sched_h=no ac_cv_func_posix_spawn=no ac_cv_func_posix_spawnp=no \
                    ac_cv_func_forkpty=no ac_cv_lib_util_forkpty=no ac_cv_func_getgroups=no \
                    ac_cv_func_system=no ac_cv_func_dup3=no ac_cv_func_pipe2=no \
                    ac_cv_func_fork=no ac_cv_func_fork1=no ac_cv_func_vfork=no \
                    ac_cv_func_getentropy=yes \
                    ac_cv_func_pthread_getname_np=no ac_cv_func_pthread_get_name_np=no \
                    ac_cv_func_pthread_setname_np=no ac_cv_func_pthread_set_name_np=no \
                    ac_cv_have_getc_unlocked=no \
                    ac_cv_func_sched_setparam=no ac_cv_func_sched_setscheduler=no \
                    ac_cv_func_sched_get_priority_max=no ac_cv_func_sched_rr_get_interval=no \
                    ac_cv_func_sched_setaffinity=no \
                    ac_cv_working_openssl_ssl=yes ac_cv_working_openssl_hashlib=yes \
                    CPPFLAGS='$(CPPFLAGS) -include $(CURDIR)/ps5_compat.h -DPS5_CLEANROOM_STDIO=1 -DPS5_PYSOCKET=1 -I$(CURDIR)'
endif

# configuration settings
CONFIGURE=./configure --prefix=$(PREFIX) \
                      --disable-shared \
                      --without-ensurepip \
                      --disable-framework \
                      --without-pymalloc \
                      --enable-ipv6 \
                      --with-build-python=$(NATIVEPREFIX)/bin/python3 \
                      --with-system-expat=yes \
                      --disable-test-modules \
                      MODULE_BUILDTYPE=static \
                      $(PY_MODULES) \
                      $(EXTRA_CONFIGURE)

# Use LIBS as some platforms (webos) require iconv to be late on the linker line
# appending to LDFLAGS puts the iconv link argument too early, causing link failures
export LIBS=$(LINK_ICONV)

LIBDYLIB=$(PLATFORM)/libpython$(PYTHON_VERSION).a

all: .installed-$(PLATFORM)

.configured-$(PLATFORM): $(DEPS) | $(TARBALLS_LOCATION)/$(ARCHIVE).$(HASH_TYPE)
	rm -rf $(PLATFORM)/*; mkdir -p $(PLATFORM)
	cd $(PLATFORM); $(ARCHIVE_TOOL) $(ARCHIVE_TOOL_FLAGS) $(TARBALLS_LOCATION)/$(ARCHIVE)
	cd $(PLATFORM); patch -p1 -i ../apple.patch
	cd $(PLATFORM); patch -p1 -i ../static-compile.patch
	cd $(PLATFORM); patch -p1 -i ../process-vm-ready.patch

ifeq ($(OS),android)
	cd $(PLATFORM); patch -p1 -i ../10-android-modules.patch
	cd $(PLATFORM); patch -p1 -i ../11-android-ctypes.patch
endif
ifeq ($(OS),wasm)
	cd $(PLATFORM); patch -p1 -i ../13-wasm-em-config-clang.patch
	cd $(PLATFORM); patch -p1 -i ../14-wasm-build-wasm-default.patch
	cd $(PLATFORM); patch -p1 -i ../15-wasm-static-hacl.patch
endif
ifeq ($(OS),ps5)
	cd $(PLATFORM); patch -p1 -i ../20-ps5-freebsd-cross-host.patch
	cd $(PLATFORM); patch -p1 -i ../21-ps5-no-subprocess.patch
	cd $(PLATFORM); patch -p1 -i ../24-ps5-socket-libscenet.patch
	cd $(PLATFORM); patch -p1 -i ../25-ps5-stdio-nomacros.patch
	# getc_unlocked() is forced off via the ac_cv_have_getc_unlocked cache
	# override above instead of a configure.ac patch - no source edit needed.
	# selectors.py's KqueueSelector no longer calls register_at_fork() in
	# this Python version (the bpo-41713 workaround it guarded was reworked
	# upstream), and every other os.register_at_fork() call site already
	# checks hasattr(os, "fork")/"register_at_fork" first, which is False
	# here since ac_cv_func_fork=no - so no patch is needed for that either.
endif
ifeq ($(findstring iphone,$(TARGET_PLATFORM)),iphone)
# These three patches are all merged upstream now (python/cpython#156110,
# #156116, #156113); drop them when we bump python past 3.16.
	cd $(PLATFORM); patch -p1 -i ../16-ios-static-nonframework.patch
	cd $(PLATFORM); patch -p1 -i ../17-ios-simulator-version-min.patch
	cd $(PLATFORM); patch -p1 -i ../18-ios-objc-fallback.patch
endif
ifeq ($(findstring appletv,$(TARGET_PLATFORM)),appletv)
	# TODO: drop once tvOS is built as a real Python platform
	cd $(PLATFORM); patch -p1 -i ../19-tvos-disable-fork-exec.patch
endif
	cd $(PLATFORM); $(AUTORECONF)
	cd $(PLATFORM); $(CONFIGURE)
	touch $@

$(LIBDYLIB): .configured-$(PLATFORM)
	$(MAKE) -C $(PLATFORM) libpython$(PYTHON_VERSION).a
	touch $@

ifeq ($(OS),ps5)
# "make install" (and the "libinstall"/"sharedinstall" targets it pulls in)
# depends on building and linking the target's standalone "python"
# executable. That link can never succeed here: the ps5_* socket symbols
# (see ps5_pysocket.h) only resolve at Kodi's own final app link, via
# libkodishim, and the PS5 SDK libc has no explicit_bzero. A title can't
# exec a standalone interpreter anyway, so - like the upstream pacbrew
# build this was ported from - install libpython/headers/stdlib by hand
# instead of running the target's own "install".
.installed-$(PLATFORM): $(LIBDYLIB)
	$(MAKE) -C $(PLATFORM) pybuilddir.txt
	$(MAKE) -C $(PLATFORM) inclinstall
	install -d $(PREFIX)/lib/pkgconfig
	install -m644 $(PLATFORM)/Misc/python.pc $(PREFIX)/lib/pkgconfig/python-$(PYTHON_VERSION).pc
	install -m644 $(PLATFORM)/Misc/python-embed.pc $(PREFIX)/lib/pkgconfig/python-$(PYTHON_VERSION)-embed.pc
	install -m644 $(PLATFORM)/libpython$(PYTHON_VERSION).a $(PREFIX)/lib/libpython$(PYTHON_VERSION).a
	mkdir -p $(PREFIX)/lib/python$(PYTHON_VERSION)
	(cd $(PLATFORM)/Lib && tar --exclude=test --exclude=tests \
		--exclude=idlelib --exclude=tkinter --exclude=turtledemo \
		--exclude=ensurepip --exclude=__pycache__ -cf - .) | \
		tar -xf - -C $(PREFIX)/lib/python$(PYTHON_VERSION)
	cp $(PLATFORM)/`cat $(PLATFORM)/pybuilddir.txt`/_sysconfigdata*.py $(PREFIX)/lib/python$(PYTHON_VERSION)/
	find $(PREFIX)/lib/python$(PYTHON_VERSION) -type f -name "*.pyc" -delete
	touch $(LIBDYLIB)
	touch $@
else
.installed-$(PLATFORM): $(LIBDYLIB)
# We specifically use -j1 as some threading issues can occur with install directory creation
	$(MAKE) -C $(PLATFORM) $(HOSTPLATFORM) install -j1
	find $(PREFIX)/lib/python$(PYTHON_VERSION) -type f -name "*.pyc" -delete
	find $(PREFIX)/lib/python$(PYTHON_VERSION) -type d -name "config-$(PYTHON_VERSION)-*" -exec rm -r {} +
	touch $(LIBDYLIB)
	touch $@
endif

clean:
	rm -rf .installed-$(PLATFORM) .configured-$(PLATFORM)

distclean::
	rm -rf $(PLATFORM) .installed-$(PLATFORM) .configured-$(PLATFORM)
