#include "proto.h"

static const char HEX[] = "0123456789ABCDEF";

void pl_begin(pline_t *l, char *dst, size_t cap)
{
    l->start = dst;
    l->p = dst;
    l->end = dst + cap - 5;  // keep room for "*CS\r\n"
    l->cs = 0;
    *l->p++ = '$';
}

void pl_char(pline_t *l, char c)
{
    if (l->p < l->end) {
        *l->p++ = c;
        l->cs ^= (uint8_t)c;
    }
}

void pl_str(pline_t *l, const char *s)
{
    while (*s) pl_char(l, *s++);
}

void pl_u64(pline_t *l, uint64_t v)
{
    char tmp[20];
    int n = 0;
    do {
        tmp[n++] = (char)('0' + (v % 10));
        v /= 10;
    } while (v);
    while (n) pl_char(l, tmp[--n]);
}

void pl_i64(pline_t *l, int64_t v)
{
    if (v < 0) {
        pl_char(l, '-');
        pl_u64(l, (uint64_t)(-(v + 1)) + 1u);
    } else {
        pl_u64(l, (uint64_t)v);
    }
}

void pl_fixed(pline_t *l, int64_t value, int decimals)
{
    if (decimals <= 0) {
        pl_i64(l, value);
        return;
    }
    if (value < 0) {
        pl_char(l, '-');
        value = -value;
    }
    int64_t scale = 1;
    for (int i = 0; i < decimals; i++) scale *= 10;
    pl_u64(l, (uint64_t)(value / scale));
    pl_char(l, '.');
    int64_t frac = value % scale;
    for (int64_t d = scale / 10; d > 0; d /= 10) {
        pl_char(l, (char)('0' + (frac / d) % 10));
    }
}

size_t pl_end(pline_t *l)
{
    *l->p++ = '*';
    *l->p++ = HEX[l->cs >> 4];
    *l->p++ = HEX[l->cs & 0x0F];
    *l->p++ = '\r';
    *l->p++ = '\n';
    return (size_t)(l->p - l->start);
}
