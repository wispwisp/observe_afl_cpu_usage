// Minimal AFL test target: aborts on b"bug!" prefix on stdin.
// The mixing loop gives short-lived target processes a non-trivial CPU
// signature so the monitor's top-N has something interesting to report.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>

int main() {
    char buf[64] = {0};
    ssize_t n = read(0, buf, sizeof(buf) - 1);
    if (n >= 4 && std::memcmp(buf, "bug!", 4) == 0) {
        std::abort();
    }
    volatile unsigned x = 0;
    for (int i = 0; i < 1024; ++i) {
        x ^= static_cast<unsigned>(buf[i & 63]) * 2654435761u;
    }
    return static_cast<int>(x) & 1;
}
