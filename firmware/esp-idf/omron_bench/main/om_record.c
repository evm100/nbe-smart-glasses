// Record decoding for the HEM-7150T. No ESP-IDF dependencies, so it is also built and checked on a
// PC against omblepy's Python (host_test/check_records.py).
#include <stdio.h>

#include "omron_ble.h"

// ---- records (hem-7150t.py deviceSpecific_ParseRecordFormat) ------------------------------------
// _bytearrayBitsToInt with little-endian: the record is one 128-bit little-endian integer and bit
// index 0 is its most significant bit.
static int bits(const uint8_t r[16], int first, int last)
{
    int v = 0;
    for (int i = first; i <= last; i++) {
        int p = 127 - i;
        v = (v << 1) | ((r[p / 8] >> (p % 8)) & 1);
    }
    return v;
}

bool om_parse_record(const uint8_t rec[16], om_record_t *o)
{
    bool empty = true;
    for (int i = 0; i < 16; i++)
        if (rec[i] != 0xff) empty = false;
    if (empty) return false;
    o->minute = bits(rec, 68, 73);
    o->second = bits(rec, 74, 79);
    if (o->second > 59) o->second = 59;  // can read up to 63
    o->mov = bits(rec, 80, 80);
    o->ihb = bits(rec, 81, 81);
    o->month = bits(rec, 82, 85);
    o->day = bits(rec, 86, 90);
    o->hour = bits(rec, 91, 95);
    o->year = bits(rec, 98, 103) + 2000;
    o->bpm = bits(rec, 104, 111);
    o->dia = bits(rec, 112, 119);
    o->sys = bits(rec, 120, 127) + 25;
    // omblepy drops records whose date doesn't parse
    return o->month >= 1 && o->month <= 12 && o->day >= 1 && o->day <= 31 && o->hour < 24 && o->minute < 60;
}

void om_record_time(const om_record_t *r, char *buf, int len)
{
    snprintf(buf, len, "%04d-%02d-%02d %02d:%02d:%02d", r->year, r->month, r->day, r->hour, r->minute, r->second);
}
