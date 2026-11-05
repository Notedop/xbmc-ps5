set(SOURCES PS5ImeDialog.cpp
        PS5Keyboard.cpp
        PS5InputBackend.cpp
        PeripheralBusPS5.cpp)

set(HEADERS PS5ImeDialog.h
        PS5Keyboard.h
        PS5InputBackend.h
        PeripheralBusPS5.h)

core_add_library(platform_ps5_input)

target_link_libraries(${CORE_LIBRARY} PRIVATE
        ScePad
        SceImeDialog
        SceUserService)