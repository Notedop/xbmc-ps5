/* Intermediate layout for native shared libraries. */
OUTPUT_FORMAT("elf64-x86-64")
OUTPUT_ARCH(i386:x86-64)

PHDRS {
    text PT_LOAD FLAGS(5);
    ro PT_LOAD FLAGS(4);
    relro PT_LOAD FLAGS(6);
    data PT_LOAD FLAGS(6);
    dynamic PT_DYNAMIC FLAGS(6);
    tls PT_TLS FLAGS(4);
}

SECTIONS {
    .text 0 : {
        QUAD(0xcccccccccccccccc)
        QUAD(0xcccccccccccccccc)
        *(.text .text.* .plt .plt.*)
    } :text

    . = ALIGN(0x4000);
    .rodata : {
        BYTE(0)
        *(.rodata .rodata.*)
    } :ro
    .eh_frame_hdr : { *(.eh_frame_hdr) } :ro
    .eh_frame : {
        PROVIDE_HIDDEN(__ps5_eh_frame_start = .);
        KEEP(*(.eh_frame))
        LONG(0)
    } :ro
    .gcc_except_table : { *(.gcc_except_table .gcc_except_table.*) } :ro

    . = ALIGN(0x4000);
    .ps5.module : {
        . += 0x2a8;
    } :relro
    .data.rel.ro : { *(.data.rel.ro .data.rel.ro.*) } :relro
    .got : { *(.got) } :relro
    .got.plt : { *(.got.plt) } :relro
    .init_array : {
        PROVIDE_HIDDEN(__ps5_init_array_start = .);
        KEEP(*(SORT_BY_INIT_PRIORITY(.init_array.*)))
        KEEP(*(.init_array))
        PROVIDE_HIDDEN(__ps5_init_array_end = .);
    } :relro
    .fini_array : {
        PROVIDE_HIDDEN(__ps5_fini_array_start = .);
        KEEP(*(.fini_array.00000))
        KEEP(*(SORT_BY_INIT_PRIORITY(.fini_array.*)))
        KEEP(*(.fini_array))
        PROVIDE_HIDDEN(__ps5_fini_array_end = .);
    } :relro
    .dynsym : { *(.dynsym) } :relro
    .hash : { *(.hash) } :relro
    .gnu.hash : { *(.gnu.hash) } :relro
    .dynstr : { *(.dynstr) } :relro
    .rela.dyn : { *(.rela.dyn) } :relro
    .rela.plt : { *(.rela.plt) } :relro

    . = ALIGN(0x4000);
    .dynamic : {
        *(.dynamic)
        . += 0x1000;
    } :data :dynamic
    .data : { *(.data .data.*) } :data
    .tdata : { *(.tdata .tdata.*) } :data :tls
    .tbss : { *(.tbss .tbss.*) } :data :tls
    .bss : { *(.bss .bss.* COMMON) } :data
}

PROVIDE_HIDDEN(__eh_frame_start = ADDR(.eh_frame));
PROVIDE_HIDDEN(__eh_frame_end = ADDR(.eh_frame) + SIZEOF(.eh_frame));
PROVIDE_HIDDEN(__eh_frame_hdr_start = ADDR(.eh_frame_hdr));
PROVIDE_HIDDEN(__eh_frame_hdr_end = ADDR(.eh_frame_hdr) + SIZEOF(.eh_frame_hdr));
