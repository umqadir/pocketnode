// CPython turns PyGILState_Check() into "always true" the moment a second
// interpreter exists, because its per-thread bookkeeping can't tell
// interpreters apart. Extension modules that assert the GIL is *not* held
// (PyTorch's autograd engine does) then fail for the rest of the process.
//
// pocketnode runs subinterpreter work on threads that do nothing else, so on
// every other thread the bookkeeping is exact and the check can stay on.
#define Py_BUILD_CORE 1
#include <Python/Python.h>
#include "internal/pycore_runtime.h"

__attribute__((visibility("default"), used))
void pocketnode_enable_gilstate_check(void) {
    _PyRuntime.gilstate.check_enabled = 1;
}
