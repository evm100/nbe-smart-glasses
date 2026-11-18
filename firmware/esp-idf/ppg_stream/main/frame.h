// Binary data frames for the MQTT link (see firmware/PROTOCOL.md, "MQTT data frames").
//
// The node's output is one byte stream of records; a frame carries whole records, starting at
// the stream offset given in its header. The server acknowledges offsets ("ACK <boot> <off>"),
// so the node keeps every record until it is acknowledged and can send it again.
//
//   frame   'N' 'B' | ver=2 | flags=0 | boot u32 | offset u64 | crc32 u32 (of the records) | records
//   record  type u8 | body length (uvarint) | body
//     REC_TEXT     body = one protocol v1 line, "$...*CS" without "\r\n" (H / E / S lines)
//     REC_SAMPLES  body = n u16 | seq0 | t0_us | dt_nom_us | nch u8 | first sample's values
//                         | for each further sample: zz(dt - dt_nom), zz(delta) per channel
//                  (uvarints; zz = zigzag; seq increases by 1 per sample)
//     REC_FRAME    body = sid u8 | seq u32 | t_us u64 | w u16 | h u16 | fmt u8 | exp u16 | gain u8 | pixels
//                  (protocol v2: one camera frame of stream `sid`, see PROTOCOL.md "Camera frames")
//
// USB binary link (CONFIG_PPG_LINK_USB_BIN): the same records, in envelopes on the byte stream
//   envelope  0xA5 0x5A | len u32 | crc32 u32 (of the records) | records
// (no boot/offset: USB doesn't lose bytes silently, and the gateway on the PC owns acknowledgement)
//
// All integers little-endian. A sample costs ~2-5 bytes instead of ~35 as a text line.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define FRAME_VERSION 2
#define FRAME_HDR_LEN 20
#define REC_TEXT 0x01
#define REC_SAMPLES 0x02
#define REC_FRAME 0x03
#define FRAME_REC_HDR_LEN 21  // REC_FRAME body header before the pixels
#define ENV_HDR_LEN 10
#define ENV_MAGIC0 0xA5
#define ENV_MAGIC1 0x5A
#define FMT_GRAY8 0
#define FMT_JPEG 1
#define SREC_MAX 2048  // largest samples record body (~1 s at 400 sps)
#define FRAME_MAX_CH 2

uint32_t frame_crc32(uint32_t crc, const uint8_t *p, size_t n);  // zlib / IEEE CRC-32
size_t frame_uvarint(uint8_t *dst, uint64_t v);                  // returns bytes written (<= 10)
size_t frame_get_uvarint(const uint8_t *p, size_t n, uint64_t *v);  // bytes read, 0 = incomplete
void frame_header(uint8_t *dst, uint32_t boot, uint64_t offset, const uint8_t *records, size_t len);

// A samples record being built: samples are appended until the next one doesn't continue it.
typedef struct {
    uint8_t body[SREC_MAX];
    size_t len;  // 0 = empty
    uint16_t n;
    uint8_t nch;
    uint32_t next_seq, dt_nom;
    int64_t last_t;
    uint32_t last[FRAME_MAX_CH];
} srec_t;

// Appends one sample; false = it doesn't continue the record (seq gap, other channels / rate,
// full): write out the record (srec_emit) and add the sample again.
bool srec_add(srec_t *r, uint32_t seq, int64_t t_us, const uint32_t *v, int nch, uint32_t dt_nom_us);
// Writes the record (type + length + body) to dst, which needs srec_size(r) bytes, and empties it.
size_t srec_size(const srec_t *r);
size_t srec_emit(srec_t *r, uint8_t *dst);
// A text record (line without "\r\n"); dst needs len + 4 bytes.
size_t rec_text(uint8_t *dst, const char *line, size_t len);
// Start of a frame record: type + length + the 21-byte body header (the pixels follow, data_len
// bytes). dst needs 6 + FRAME_REC_HDR_LEN bytes; returns bytes written.
size_t rec_frame_head(uint8_t *dst, uint8_t sid, uint32_t seq, int64_t t_us, uint16_t w, uint16_t h, uint8_t fmt,
                      uint16_t exposure, uint8_t gain, size_t data_len);
// USB envelope header for `len` bytes of records whose CRC-32 is `crc` (frame_crc32(0, ...)).
void env_header(uint8_t *dst, uint32_t len, uint32_t crc);
