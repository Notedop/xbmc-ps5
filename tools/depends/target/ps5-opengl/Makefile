ROOT := $(abspath ../../../..)
WORK ?= $(ROOT)/build/ps5-external
PS5_OPENGL_SOURCE ?= $(WORK)/ps5-opengl
PS5_OPENGL_PREFIX ?= /opt/ps5-opengl-gl46
PS5_PAYLOAD_SDK ?= /opt/ps5-payload-sdk
JOBS ?= 1

# The backend owns its source, build and installation receipts.
.PHONY: all clean distclean
all:
	WORK="$(WORK)" PS5_OPENGL_SOURCE="$(PS5_OPENGL_SOURCE)" \
	  PS5_OPENGL_PREFIX="$(PS5_OPENGL_PREFIX)" PS5_PAYLOAD_SDK="$(PS5_PAYLOAD_SDK)" \
	  JOBS="$(JOBS)" bash "$(ROOT)/tools/ps5/bootstrap/ps5-opengl.sh"

clean distclean:
	@echo "Preserving the external PS5 OpenGL cache at $(PS5_OPENGL_SOURCE)"
