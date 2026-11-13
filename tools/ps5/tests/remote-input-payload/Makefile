PS5_PAYLOAD_SDK ?= /opt/ps5-payload-sdk
PS5_HOST ?= ps5
PS5_PORT ?= 9021

include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk

ELF := remote-input-payload.elf
CFLAGS := -std=c11 -Wall -Wextra -Werror -O2 -g

.PHONY: all test clean

all: $(ELF)

$(ELF): main.c
	$(CC) $(CFLAGS) -o $@ $< -lScePad -lSceUserService -lSceMbus -lpthread

remote-init-state.elf: init-state.c
	$(CC) $(CFLAGS) -o $@ $< -lSceMbus

test-state: remote-init-state.elf
	$(PS5_DEPLOY) -h $(PS5_HOST) -p $(PS5_PORT) $<

test: $(ELF)
	$(PS5_DEPLOY) -h $(PS5_HOST) -p $(PS5_PORT) $<

clean:
	rm -f remote-input-payload.elf remote-init-state.elf
