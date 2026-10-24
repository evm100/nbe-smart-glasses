#include "frame.h"

#include <string.h>

uint32_t frame_crc32(uint32_t crc, const uint8_t *p, size_t n)
{
    crc = ~crc;
    while (n--) {
        crc ^= *p++;
        for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

size_t frame_uvarint(uint8_t *dst, uint64_t v)
{
    size_t n = 0;
    while (v >= 0x80) {
        dst[n++] = (uint8_t)(v | 0x80);
        v >>= 7;
    }
    dst[n++] = (uint8_t)v;
    return n;
}

size_t frame_get_uvarint(const uint8_t *p, size_t n, uint64_t *v)
{
    uint64_t x = 0;
    for (size_t i = 0; i < n && i < 10; i++) {
        x |= (uint64_t)(p[i] & 0x7F) << (7 * i);
        if (!(p[i] & 0x80)) {
            *v = x;
            return i + 1;
        }
    }
    return 0;
}

static uint64_t zigzag(int64_t v)
{
    return ((uint64_t)v << 1) ^ (uint64_t)(v >> 63);
}

static void put_le(uint8_t *dst, uint64_t v, int bytes)
{
    for (int i = 0; i < bytes; i++) dst[i] = (uint8_t)(v >> (8 * i));
}

void frame_header(uint8_t *dst, uint32_t boot, uint64_t offset, const uint8_t *records, size_t len)
{
    dst[0] = 'N';
    dst[1] = 'B';
    dst[2] = FRAME_VERSION;
    dst[3] = 0;
    put_le(dst + 4, boot, 4);
    put_le(dst + 8, offset, 8);
    put_le(dst + 16, frame_crc32(0, records, len), 4);
}

bool srec_add(srec_t *r, uint32_t seq, int64_t t_us, const uint32_t *v, int nch, uint32_t dt_nom_us)
{
    // worst case per sample: 10 (dt) + 10 per channel
    if (r->len && (seq != r->next_seq || nch != r->nch || dt_nom_us != r->dt_nom || r->n == UINT16_MAX ||
                   r->len + 10 + 10 * (size_t)nch > SREC_MAX)) {
        return false;
    }
    uint8_t *p = r->body + r->len;
    if (!r->len) {  // header: n (patched as samples are added), seq0, t0, dt_nom, nch, first values
        p += 2;
        p += frame_uvarint(p, seq);
        p += frame_uvarint(p, (uint64_t)(t_us < 0 ? 0 : t_us));
        p += frame_uvarint(p, dt_nom_us);
        *p++ = (uint8_t)nch;
        for (int c = 0; c < nch; c++) p += frame_uvarint(p, v[c]);
        r->nch = (uint8_t)nch;
        r->dt_nom = dt_nom_us;
        r->n = 0;
    } else {
        p += frame_uvarint(p, zigzag(t_us - r->last_t - (int64_t)dt_nom_us));
        for (int c = 0; c < nch; c++) p += frame_uvarint(p, zigzag((int64_t)v[c] - (int64_t)r->last[c]));
    }
    r->len = (size_t)(p - r->body);
    r->n++;
    put_le(r->body, r->n, 2);
    r->next_seq = seq + 1;
    r->last_t = t_us;
    for (int c = 0; c < nch; c++) r->last[c] = v[c];
    return true;
}

size_t srec_size(const srec_t *r)
{
    uint8_t tmp[10];
    return r->len ? 1 + frame_uvarint(tmp, r->len) + r->len : 0;
}

size_t srec_emit(srec_t *r, uint8_t *dst)
{
    if (!r->len) return 0;
    size_t n = 0;
    dst[n++] = REC_SAMPLES;
    n += frame_uvarint(dst + n, r->len);
    memcpy(dst + n, r->body, r->len);
    n += r->len;
    r->len = 0;
    return n;
}

size_t rec_text(uint8_t *dst, const char *line, size_t len)
{
    size_t n = 0;
    dst[n++] = REC_TEXT;
    n += frame_uvarint(dst + n, len);
    memcpy(dst + n, line, len);
    return n + len;
}
