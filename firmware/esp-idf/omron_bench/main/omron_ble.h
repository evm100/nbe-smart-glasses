// Omron HEM-7150T (BP7250) over BLE: a C port of omblepy's protocol (auto-cuff/omblepy).
//
// Service ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b, 4 TX + 4 RX channels of 16 bytes each, an unlock
// characteristic for the 16-byte pairing key, and EEPROM block reads for the records.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define OM_MAX_RECORDS 60  // HEM-7150T: one user, 60 records

typedef struct {
    int year, month, day, hour, minute, second;
    int sys, dia, bpm;
    int mov, ihb;  // body-movement / irregular-heartbeat flags
} om_record_t;

// Start NimBLE and wait until the host is ready.
esp_err_t om_init(void);

// One-time pairing. The monitor must be in pairing mode (hold its Bluetooth button until "P"
// blinks). Bonds at the BLE level, then programs `key` as the unlock key, as `omblepy -p` does.
esp_err_t om_pair(const uint8_t mac[6], const uint8_t key[16]);

// Connect, unlock with `key`, read every stored record, disconnect. The monitor only advertises
// for a short while after a reading (or after pressing its Bluetooth button). `scan_ms` = how long
// to look for it. Records come back in ring-buffer order (unsorted); empty slots are skipped.
esp_err_t om_read(const uint8_t mac[6], const uint8_t key[16], int scan_ms,
                  om_record_t *out, int max, int *n_out);

// Delete every stored BLE bond (the monitor then needs `pair` again).
void om_forget(void);
int om_bond_count(void);

// Human-readable reason for the last failure.
const char *om_last_error(void);

// Decode one 16-byte record (exposed for tests). false for an empty (all 0xFF) slot.
bool om_parse_record(const uint8_t rec[16], om_record_t *out);

// "YYYY-MM-DD HH:MM:SS": sorts by time, the same format omblepy writes to its CSV.
void om_record_time(const om_record_t *r, char *buf, int len);
