extern int puts(const char*);
int host_object = 42;
int host_definition(void) { return host_object; }
int (*host_pointer)(void) = host_definition;
void _start(void) { puts("native host export fixture"); }
