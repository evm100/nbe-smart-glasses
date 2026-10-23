// PPG serial protocol v1 line builder:  $<body>*<CS>\r\n   (CS = XOR of body bytes)
// No printf: fast at 400+ lines/s and independent of libc int64 printf support.
#pragma once

#include <stddef.h>
#include <stdint.h>

#define PROTO_MAX_LINE 256

typedef struct {
    char *start;  // where '$' was written
    char *p;      // write position
    char *end;    // one past the last usable byte (room for "*CS\r\n" is reserved)
    uint8_t cs;
} pline_t;

// Start a line at dst (needs at least PROTO_MAX_LINE bytes free).
void pl_begin(pline_t *l, char *dst, size_t cap);
void pl_char(pline_t *l, char c);
void pl_str(pline_t *l, const char *s);
void pl_u64(pline_t *l, uint64_t v);
void pl_i64(pline_t *l, int64_t v);
// fixed-point: value / 10^decimals, e.g. pl_fixed(l, 64, 1) -> "6.4"
void pl_fixed(pline_t *l, int64_t value, int decimals);
// Append "*CS\r\n"; returns total line length in bytes.
size_t pl_end(pline_t *l);
