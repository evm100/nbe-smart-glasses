// See logfwd.h.
#include "sdkconfig.h"

#if CONFIG_PPG_FORWARD_LOGS
#include "logfwd.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"

#define LOG_LINE_MAX 112
#define QUEUE_N 32
#define STASH_N 40

// All in DRAM and guarded by a spinlock: rom_putc can run with the flash cache disabled or from an ISR.
static DRAM_ATTR portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static DRAM_ATTR char s_q[QUEUE_N][LOG_LINE_MAX];
static DRAM_ATTR uint32_t s_q_head, s_q_tail;  // free-running counters
static DRAM_ATTR uint32_t s_lost;
static DRAM_ATTR char s_stash[STASH_N][LOG_LINE_MAX];
static DRAM_ATTR uint32_t s_stash_n;
static DRAM_ATTR volatile bool s_booting = true;
static DRAM_ATTR char s_rom_line[LOG_LINE_MAX];  // ROM printf assembles one char at a time
static DRAM_ATTR uint32_t s_rom_len;
static vprintf_like_t s_prev_vprintf;

// Copy one line in (spinlock held): to the queue if `live`, to the boot stash while booting.
// Printable ASCII only, protocol framing characters replaced.
static void IRAM_ATTR put_line_locked(const char *s, uint32_t n, bool live)
{
    while (n && (s[n - 1] == '\r' || s[n - 1] == '\n' || s[n - 1] == ' ')) n--;
    if (!n) return;
    if (n > LOG_LINE_MAX - 1) n = LOG_LINE_MAX - 1;
    char *dst = NULL;
    if (live) {
        if (s_q_head - s_q_tail >= QUEUE_N) s_lost++;
        else dst = s_q[s_q_head % QUEUE_N];
    }
    char *st = (s_booting && s_stash_n < STASH_N) ? s_stash[s_stash_n++] : NULL;
    for (uint32_t i = 0; i < n; i++) {
        char c = s[i];
        if (c < 0x20 || c > 0x7E || c == '$' || c == '*') c = ' ';
        if (dst) dst[i] = c;
        if (st) st[i] = c;
    }
    if (dst) {
        dst[n] = 0;
        s_q_head++;
    }
    if (st) st[n] = 0;
}

// ROM printf channel 2 (ets_printf / esp_rom_printf), one character at a time.
static void IRAM_ATTR rom_putc(char c)
{
    portENTER_CRITICAL_SAFE(&s_mux);
    if (c == '\n') {
        put_line_locked(s_rom_line, s_rom_len, true);
        s_rom_len = 0;
    } else if (s_rom_len < LOG_LINE_MAX - 1) {
        s_rom_line[s_rom_len++] = c;
    }
    portEXIT_CRITICAL_SAFE(&s_mux);
}

// ESP_LOGx: format once; W/E go out live, every level goes to the boot stash while booting; then
// the UART console gets it as before.
static int log_vprintf(const char *fmt, va_list ap)
{
    va_list ap2;
    va_copy(ap2, ap);
    char buf[128];
    int n = vsnprintf(buf, sizeof buf, fmt, ap2);
    va_end(ap2);
    if (n > 0) {
        if (n > (int)sizeof buf - 1) n = sizeof buf - 1;
        bool live = buf[0] == 'W' || buf[0] == 'E';
        if (live || s_booting) {
            portENTER_CRITICAL_SAFE(&s_mux);
            const char *p = buf, *end = buf + n;
            while (p < end) {  // a message can hold several lines
                const char *nl = memchr(p, '\n', (size_t)(end - p));
                const char *e = nl ? nl : end;
                put_line_locked(p, (uint32_t)(e - p), live);
                p = nl ? nl + 1 : end;
            }
            portEXIT_CRITICAL_SAFE(&s_mux);
        }
    }
    return s_prev_vprintf ? s_prev_vprintf(fmt, ap) : vprintf(fmt, ap);
}

void logfwd_install(void)
{
    s_prev_vprintf = esp_log_set_vprintf(log_vprintf);
    esp_rom_install_channel_putc(2, rom_putc);  // channel 1 stays the UART console
}

void logfwd_boot_done(void)
{
    s_booting = false;
}

bool logfwd_next(char *buf, int size)
{
    bool got = false;
    portENTER_CRITICAL_SAFE(&s_mux);
    if (s_q_tail != s_q_head) {
        strlcpy(buf, s_q[s_q_tail % QUEUE_N], (size_t)size);
        s_q_tail++;
        got = true;
    }
    portEXIT_CRITICAL_SAFE(&s_mux);
    return got;
}

bool logfwd_boot_line(int i, char *buf, int size)
{
    bool got = false;
    portENTER_CRITICAL_SAFE(&s_mux);
    if (i >= 0 && (uint32_t)i < s_stash_n) {
        strlcpy(buf, s_stash[i], (size_t)size);
        got = true;
    }
    portEXIT_CRITICAL_SAFE(&s_mux);
    return got;
}

uint32_t logfwd_lost(void)
{
    return s_lost;
}
#endif
