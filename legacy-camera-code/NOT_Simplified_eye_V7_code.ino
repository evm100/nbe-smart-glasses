/*
 * NOT_Simplified_eye_V7_code.ino
 * Created: 2026-010-28
 *
 * V4 + heart rate (MAX30102) + non-contact temperature (MLX90614), all on the
 * one 2 Mbaud link. Based on Simplified_eye_V4_code.ino — the camera and LDR
 * paths are UNCHANGED. This is additive.
 *
 * Merges in:
 *   Simplified_eye_V4_code.ino  (camera + LDR + IR LEDs)
 *   Heart_Waveform.ino          (MAX30102 PPG, filters carried over verbatim)
 *   MLX90614_Stream.ino         (ambient + object temperature)
 *
 * Pairs with Simplified_eye_V5.m.
 *
 * ---------------------------------------------------------------------------
 * SERIAL PROTOCOL (2 Mbaud) — FOUR packet types, distinguished by byte [2]
 *
 *   CAMERA FRAME  {255,0,255,0} + 6400 bytes (80 rows x 80 px, row-major)
 *   LDR READING   {255,0,254,0} + 2 bytes   uint16 LE
 *   TEMPERATURE   {255,0,253,0} + 4 bytes   int16 LE ambient, int16 LE object,
 *                                           both in HUNDREDTHS of a degree C
 *   HEART BATCH   {255,0,252,0} + 1 byte N, then N x 4 bytes:
 *                                           int16 LE filtered, int16 LE derivative
 *   STATUS        {255,0,251,0} + 1 byte bitmask, 1 Hz:
 *                   bit0 camera  bit1 LDR (always 1)  bit2 heart
 *                   bit3 temp    bit4 IR LED currently on
 *
 *   Bytes [0],[1],[3] are identical in all five; only byte [2] differs.
 *   MATLAB reads 4 header bytes, switches on byte [2], then reads the payload
 *   that header implies.
 *
 *   NOTHING ELSE MAY BE WRITTEN TO Serial ONCE STREAMING STARTS. A stray
 *   Serial.println would be consumed as pixel data and tear the frame. This is
 *   why the heart sensor's no-finger case sends a binary 0,0 sample here
 *   instead of Heart_Waveform.ino's literal "0,0" text, and why a missing
 *   sensor disables itself silently rather than printing in a loop.
 *
 *   Packets are never interleaved: each is written in one uninterrupted burst
 *   inside loop(), so a header is always followed by exactly its own payload.
 *
 *   WHY HEART SAMPLES ARE BATCHED: the PPG runs at 100 Hz. One packet per
 *   sample would be 100 headers/s for MATLAB to parse on top of ~25 frames/s.
 *   Draining the sensor FIFO once per loop and sending one packet gives ~3-4
 *   samples per packet and a quarter of the parsing overhead, for 4 extra bytes.
 * ---------------------------------------------------------------------------
 *
 * MATLAB -> ESP32 LED CONTROL (unchanged): 0xF1 = IR LEDs ON, 0xF0 = OFF.
 * This sketch still never switches the LEDs on its own — MATLAB classifies the
 * light and decides, so retuning thresholds never needs a reflash.
 *
 * ---------------------------------------------------------------------------
 * WIRING (XIAO ESP32S3)
 *
 *   Camera   GPIO10-18, 38-40, 47, 48  (on-board, Sense expansion)
 *   IR LEDs  D0  (GPIO1)
 *   LDR      D1  (GPIO2)   divider: 3V3 -> LDR -> junction -> 10k -> GND
 *   I2C      D4  (GPIO5) = SDA,  D5 (GPIO6) = SCL
 *            BOTH sensors share this one pair: MLX90614 at 0x5A and
 *            MAX30102 at 0x57. Different addresses, one bus. See the
 *            I2C TOPOLOGY note below for why that is mandatory.
 *
 *   The IR LED pin must never be a D4/D5 GPIO. Setting it to 6 (which is D5,
 *   not D6) drives SCL as an output and takes the whole bus down with it.
 *
 * ---------------------------------------------------------------------------
 * OPEN PROBLEM — THE CAMERA AND THE I2C SENSORS DO NOT COEXIST (2026-08-21)
 *
 *   Measured, both directions:
 *     ENABLE_CAMERA 1 -> boot probe brings Wire(bus0) up fine on D4/D5, sweeps
 *                        all 127 addresses, and finds NOTHING. No temperature,
 *                        no heart.
 *     ENABLE_CAMERA 0 -> both sensors answer immediately and stream normally.
 *
 *   The bus itself is healthy in both cases — begin() succeeds, the scan runs.
 *   Devices simply stop acknowledging once frames are streaming. That is not a
 *   pin conflict (the camera touches none of GPIO1/2/5/6) and not a port
 *   conflict (bus0 is ours, SCCB holds bus1, and the probe says so).
 *
 *   Leading suspect: the 3V3 rail. The OV2640 pulls well over 100 mA while
 *   streaming, on top of two breakouts and their pull-ups. A sagging rail stops
 *   an I2C device ACKing long before it stops a camera capturing.
 *
 *   TO CONFIRM: power both breakouts from a separate 3V3 supply with a COMMON
 *   GROUND to the XIAO, leave the signal lines on D4/D5, and re-run with
 *   ENABLE_CAMERA 1. If they answer, it is supply current and no code change
 *   will fix it.
 * ---------------------------------------------------------------------------
 *
 *   *** EVERY SENSOR RUNS FROM 3V3, NEVER 5V ***
 *   The I2C pull-ups on both breakouts tie SDA/SCL to VIN. At 5V those lines
 *   idle above the ESP32's 3.6V absolute maximum and will damage the GPIO.
 *   Same applies to the LDR divider.
 *
 * I2C TOPOLOGY — ONE BUS ONLY, WORKED OUT AT BOOT
 *   *** BOTH I2C SENSORS MUST SHARE ONE PIN PAIR. ***
 *
 *   The ESP32-S3 has exactly TWO I2C peripherals (SOC_I2C_NUM = 2), and the
 *   camera's SCCB permanently owns one of them — the esp32-camera driver keeps
 *   a global literally named sccb_owns_i2c_port. Arduino hardcodes Wire to
 *   bus 0 and Wire1 to bus 1. So "camera + Wire + Wire1" is three buses on a
 *   two-bus chip and one of them always loses.
 *
 *   That is not a style preference, it is a hardware limit: a sensor wired to a
 *   different pin pair than the other CANNOT be reached while the camera runs.
 *   Put the MLX90614 and the MAX30102 on the SAME two pins.
 *
 *   Which port SCCB grabs is not knowable up front, so at boot this sketch
 *   probes BOTH bus objects (Wire, then Wire1) against the one pin pair above
 *   and keeps whichever answers. A bus already owned by the camera reports
 *   itself by failing to begin(). The probe table is printed in the boot
 *   report — read it to see exactly what was found where.
 *
 *   The second candidate pin pair (D2/D3) was removed once the wiring settled:
 *   both sensors now share D4/D5, so there was nothing left for it to find.
 *
 *   The bus runs at 100 kHz. See I2C_BUS_HZ.
 *
 * REQUIRES: Tools > PSRAM = "OPI PSRAM", or esp_camera_init fails.
 * LIBRARIES: SparkFun MAX3010x, Adafruit MLX90614, Adafruit BusIO.
 * ---------------------------------------------------------------------------
 */

/*
 * ===========================================================================
 * BISECT SWITCH — set to 0 to build the camera + LDR only (i.e. V4 behaviour
 * from this same file), with the heart and temperature code compiled out.
 *
 * Use it if esp_camera_init fails. See the CAMERA FAILURE note below.
 * ===========================================================================
 */
#define ENABLE_I2C_SENSORS 1

/*
 * ===========================================================================
 * CAMERA SWITCH — set to 0 for a sensors-only build in which esp_camera_init()
 * is NEVER CALLED. Not just the frame send: the whole camera path is out, so
 * SCCB never creates or destroys an I2C master bus.
 *
 * That distinction is the point. Camera init churns the I2C subsystem whether
 * it succeeds or fails, and with no camera attached it does so twice via the
 * retry. Setting this to 0 is the clean test for wheher that churn is what
 * leaves the sensors unreachable.
 * ===========================================================================
 */
#define ENABLE_CAMERA 1

/*
 * ===========================================================================
 * HEART DIAGNOSTIC — set to 1 to publish the MAX30102's RAW counts instead of
 * the filtered waveform. The DSP still runs (so its state stays valid), but
 * what reaches MATLAB is the unprocessed signal, scaled >>3 to fit int16.
 *
 * This answers one question that no amount of reading the filter code can:
 * is the sensor producing a varying signal at all? With this at 1 the upper
 * panel shows the live channel and the lower one shows the other channel, so
 * a swap is visible too.
 *
 *   trace MOVES with a finger -> sensor and transport are fine, the DSP is
 *                                the problem
 *   trace FLAT with a finger  -> the sensor is not producing usable variation
 *                                at these ADC_RANGE / LED_BRIGHTNESS settings
 *
 * Set back to 0 for real sessions — raw counts are not a PPG.
 * ===========================================================================
 */
#define HEART_SEND_RAW 0

/*
 * ===========================================================================
 * PPG CENSUS — set to 1 to run the 8-second boot-time signal measurement.
 *
 * OFF by default. It did its job (it found the ADC_RANGE saturation), but it
 * holds the board SILENT for ~8 s at boot — 3 s countdown plus 5 s measuring —
 * on top of the USB CDC settle and the boot report. MATLAB's sync gives up if
 * the board says nothing for long enough, so leaving this on made startup a
 * race that failed most of the time.
 *
 * Turn it back on only when you need to re-measure DC level and AC p-p.
 * ===========================================================================
 */
#define PPG_CENSUS 0

#include "esp_camera.h"
#if ENABLE_I2C_SENSORS
  #include <Wire.h>
  #include <Adafruit_MLX90614.h>
  #include "MAX30105.h"
#endif

// --- 1. CAMERA PINS (XIAO ESP32S3 Sense) ---
#define PWDN_GPIO_NUM  -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM  10
#define SIOD_GPIO_NUM  40
#define SIOC_GPIO_NUM  39
#define Y9_GPIO_NUM    48
#define Y8_GPIO_NUM    11
#define Y7_GPIO_NUM    12
#define Y6_GPIO_NUM    14
#define Y5_GPIO_NUM    16
#define Y4_GPIO_NUM    18
#define Y3_GPIO_NUM    17
#define Y2_GPIO_NUM    15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM  47
#define PCLK_GPIO_NUM  13

// --- 2. ROI PARAMETERS ---
const int ROI_SIZE = 80;    // Size of the eye-box
const int IMG_W = 240;      // Original Camera Width
const int START_X = (IMG_W - ROI_SIZE) / 2;
const int START_Y = (IMG_W - ROI_SIZE) / 2;

// --- 3. IR LED PIN ---
// D0 = GPIO1. Pin numbers here are GPIO numbers, NOT D-numbers. From the core's
// own variant file, variants/XIAO_ESP32S3/pins_arduino.h:
//   D0=1  D1=2  D2=3  D3=4  D4=5  D5=6  D6=43
//
// History worth keeping: this was briefly set to 6, which is NOT D6 — GPIO6 is
// D5, the I2C SCL line both sensors share. Driving SCL as a digital output
// takes the whole bus down. It then went to 43 (the real D6, U0TXD, usable
// because Serial runs over native USB CDC), and is now back on D0/GPIO1 where
// the wiring reaches and where it is known to have worked.
//
// GPIO1 is ADC1_CH0. Used as a plain digital output here, which is fine — but
// it means the LDR must NOT also be on D0. It is on D1/GPIO2.
#define LED_GPIO_NUM 1     // D0 = GPIO1 (moved back from D6/GPIO43)

// --- 4. EXPOSURE/GAIN LOCK ---
// Bench-tuned WITH THE IR LEDs ON and the camera a couple of cm from the eye.
// That is a brightly lit close-up scene, so these are deliberately low. Point
// the same settings at an unlit eye and the image is nearly black — which is
// not a fault, it is the exposure doing what it was told.
#define FIXED_AEC_VALUE 300    // OV2640 exposure, roughly 0..1200
#define FIXED_AGC_GAIN  2      // manual gain (0..30); low = cleaner image

// DIAGNOSTIC SWITCH — set to 1 to hand exposure and gain back to the sensor's
// own auto algorithm. Use it to answer one question: "is the image dark because
// the camera is broken, or because my fixed exposure is too low for this
// lighting?" If auto looks correct, the sensor is fine and the fixed values
// need raising for the current illumination.
//
// Do NOT leave this at 1 for real sessions. Locking exposure is the whole point
// of the V3-era fix: under IR the auto algorithm hunts frame to frame, and the
// brightness swings ruin the blink thresholds.
#define CAMERA_AUTO_EXPOSURE 0

// --- 5. LDR ---
// D1 = GPIO2 = ADC1_CH1. ADC1 deliberately: ADC2 is unusable when WiFi is
// active, and no camera pin touches GPIO2 so there is no conflict with the
// capture path.
//
// MOVED from GPIO1 (D0). The calibrated thresholds in Simplified_eye_V5.m
// (dark 497, bright 815) were captured on GPIO1, a DIFFERENT ADC channel, so
// expect a small offset. Only DARK vs not-DARK changes behaviour (it drives the
// IR LEDs), so that is the boundary worth re-checking.
#define LDR_PIN 2

const int  SMOOTH_N      = 10;  // moving-average window (matches LDR_Characterization.ino)
const long LDR_SEND_MS   = 100; // 10 Hz
const long LDR_SAMPLE_MS = 20;  // 10 samples x 20 ms = 200 ms to fully settle

int  ldrBuf[SMOOTH_N];
int  ldrIdx = 0;
long ldrSum = 0;
unsigned long lastLdrSend   = 0;
unsigned long lastLdrSample = 0;

// --- 6. I2C SENSORS (NEW IN V5) ---
#define MLX_ADDR  0x5A
#define MAX_ADDR  0x57

// THE ONE PIN PAIR BOTH SENSORS SHARE.
//
// These were previously named I2C_A_* / I2C_B_* and commented as though each
// sensor had its own pair ("MAX30102 lives here" / "MLX90614 fallback bus").
// That was never what they did and the labels were misleading: they were two
// CANDIDATE pairs that the boot search tried, and whichever answered was then
// used by BOTH sensors. One bus, always.
//
// The pins are settled now — MLX90614 (0x5A) and MAX30102 (0x57) are both on
// D4/D5 — so the second candidate is gone. What remains is the Wire/Wire1
// search, which is still required: the camera's SCCB claims one of the chip's
// two I2C peripherals and which one it takes is not knowable up front.
#define I2C_SDA 5     // D4
#define I2C_SCL 6     // D5
 
// 100 kHz for EVERY I2C bus here, deliberately and unconditionally.
//
// The MLX90614 is an SMBus part rated for 100 kHz; 400 kHz is out of spec and
// it starts returning NaN. The MAX30102 is happy either way — at
// SAMPLE_AVERAGE 4 it emits 100 Hz x 6 bytes = ~600 B/s, about 6% of a 100 kHz
// bus, so "400 kHz or the FIFO drops samples" does not apply at this rate.
//
// This constant exists because MAX30105::begin() calls _i2cPort->setClock()
// internally. Passing it 400 kHz reconfigures the SHARED bus behind the
// MLX90614's back. That is exactly what broke temperature: with no heart
// sensor plugged in, retryMissingHardware() re-probed every 10 s, each probe
// shoved the bus back to 400 kHz, and the MLX90614 NaN'd from then on — so
// temperature read fine for a few seconds after boot and then went STALE
// forever. Never hand this library a speed that is not this constant.
#define I2C_BUS_HZ 100000

#if ENABLE_I2C_SENSORS
Adafruit_MLX90614 mlx = Adafruit_MLX90614();
MAX30105          ppg;
#endif

bool haveMlx  = false;
bool havePpg  = false;

// 2 Hz. An MLX90614 read is a BLOCKING SMBus transaction of roughly 10 ms,
// which is a visible stall against a ~30 fps frame loop. 2 Hz keeps that to
// ~2% duty and is far more than a temperature reading needs.
const long TEMP_SEND_MS = 500;
unsigned long lastTempSend = 0;

// --- 7. HEART / PPG FILTERS — re-synced with Heart_Waveform.ino 2026-08-21 ---
// The standalone sketch was debugged well past what V5 originally copied. Each
// value below carries the reason it changed; do not "tidy" them back.
//
// 400 Hz sampled, averaged by 2 on-chip -> 200 Hz out. Was averaged by 4 for
// 100 Hz. 100 Hz is ample for a PPG in signal terms (content is under ~15 Hz),
// but it made the live trace look coarse and stepped. 200 Hz doubles the
// resolution for ~1.2 kB/s of I2C traffic, about 12% of the 100 kHz shared bus.
const byte LED_BRIGHTNESS = 0x3F;  // ~12.6 mA. MEASURED, see ADC_RANGE below.
const byte SAMPLE_AVERAGE = 4;   // update as recomended by chat gpt, aug 21, 5:03 pm
const byte LED_MODE       = 2;     // 2 = Red + IR (BOTH driven, see setup)
const int  SAMPLE_RATE    = 400;
const int  PULSE_WIDTH    = 411;

// 32768 / 0x3F are MEASURED on this exact sensor, not chosen from a datasheet.
// MAX30102_RawCheck.ino walks both knobs down until the reading leaves the rail
// and reports the pair that works; this is what it settled on.
//
// Why it matters so much: 18-bit full scale is 262143, and at the old settings
// BOTH channels sat pinned there with nothing even touching the sensor. A
// pinned reading is a CONSTANT reading, and a DC blocker fed a constant outputs
// exactly zero — a dead-flat trace from a sensor that is working perfectly and
// merely being over-driven. That single fact explains the whole history here:
// the endless "0,0" from Heart_Waveform.ino, and the flat panel in V5.
//
// 16384 with LED 60 was a guess in the right direction that was still not far
// enough. If the trace ever flattens again, run RawCheck before touching
// anything else — saturation looks identical to a dead sensor from downstream.
// 16384 is the HARDWARE MAXIMUM (SPO2_ADC_RGE is 2 bits: 2048/4096/8192/16384).
//
// *** NEVER SET THIS ABOVE 16384. *** The SparkFun library maps it with:
//     else if (adcRange == 16384) setADCRange(..._16384);
//     else                        setADCRange(..._2048);
// so 32768 does not clamp — it falls through to 2048 nA, the MOST sensitive
// range there is, 8x more sensitive than intended. That is the opposite of
// what the number suggests, it fails silently, and it is what pinned a finger
// at 262143 with AC p-p exactly 0.
const int  ADC_RANGE      = 16384;

static_assert(ADC_RANGE == 2048 || ADC_RANGE == 4096 ||
              ADC_RANGE == 8192 || ADC_RANGE == 16384,
              "ADC_RANGE must be exactly 2048, 4096, 8192 or 16384. Any other "
              "value silently becomes 2048 (most sensitive) inside the library.");

// Single-pole DC blocker: w[n] = x[n] + a*w[n-1];  y[n] = w[n] - w[n-1].
// Strips the huge DC pedestal (finger IR sits around 50k-200k counts) and
// leaves the ~1% pulsatile AC component.
//
// DC_ALPHA was 0.95. The corner is roughly (1-a)*fs/(2*pi), so at 100 Hz that
// put it near 0.8 Hz = 48 bpm — directly on top of a resting pulse. The
// heartbeat was being high-passed away as though it were drift, which is the
// single biggest reason the trace looked like noise. 0.99 moves the corner far
// below any real pulse, so the pedestal still goes and the beat survives.
const float DC_ALPHA  = 0.99f;

// Light EMA after the DC blocker. Removes sensor grain without visibly
// rounding the dicrotic notch.
const float LPF_ALPHA = 0.35f;

// Display gain on the derivative only. Does not affect the filtered channel.
const int DERIV_GAIN = 1;

// --- NO FINGER DETECTION ---
// There is deliberately no presence threshold any more. It used to gate the
// whole channel — below it the sketch published 0,0 — and that gate is what
// kept the trace flat: the thresholds (5000 on, 3000 off) were chosen when
// ADC_RANGE was 4096, and at 16384 the very same light reads about 4x SMALLER,
// so a real finger could sit under 5000 forever and never open the gate.
//
// The DSP now runs on every sample and publishes unconditionally. With nothing
// on the sensor the DC blocker sees a near-constant input and outputs close to
// zero of its own accord, which is the honest answer rather than an enforced
// one. Raw magnitude is still visible in the boot report if it is ever needed.

// --- CHANNEL SELECTION ---
// The SparkFun library targets the MAX30105 (RED=LED1, IR=LED2). On many
// MAX30102 breakouts the two are swapped, so getFIFOIR() reads the dark channel
// and the host sees a flat line at a perfectly healthy data rate — the most
// confusing failure this sensor has.
//
// Both LEDs are driven and the live channel is re-decided on a slow timer from
// whichever side peaked higher over the last window. Slow on purpose: deciding
// per sample would swap mid-beat and push a step through the DC blocker that
// reads as a pulse. Defaults to IR, the standard mapping.
const uint8_t CH_NONE = 0, CH_RED = 1, CH_IR = 2;
uint8_t  chan = CH_IR;
uint32_t chanMaxRed = 0, chanMaxIr = 0;
// 5 s, and a swap needs the winner to be 25% brighter. Both guard the same
// failure: a channel that flip-flops resets the filters faster than they can
// settle, so nothing is ever published.
const unsigned long CHAN_EVAL_MS = 5000;
const float CHAN_SWITCH_MARGIN   = 1.25f;
unsigned long lastChanEval = 0;

float dcW      = 0.0f;   // DC blocker state
float lpfPrev  = 0.0f;   // EMA state
float filtPrev = 0.0f;   // previous filtered sample, for the derivative
bool  primed   = false;  // filter state valid?

// Samples to suppress after priming, so the arrival step is not published as a
// spike. 100 = 0.5 s at the 200 Hz output rate. Was 400 (2 s), which was long
// enough to swallow the trace whole whenever the filters were reset — and with
// the channel evaluation also on 2 s, that was continuous. Long enough to kill
// the step, short enough that it can never dominate.
const int SETTLE_SAMPLES = 100;
int settle = 0;

// One packet per loop iteration. At ~25 fps and 200 Hz PPG that is ~8 samples
// per packet; the cap is generous headroom for a slow frame.
const int HEART_BATCH_MAX = 48;

// Batching policy. At 200 Hz these give ~20 packets/s of ~10 samples each.
// HEART_MIN_BATCH keeps packets worth sending; HEART_MAX_WAIT_MS guarantees a
// partial batch still goes out promptly so the trace cannot stall.
// FIFO poll interval. See the POLL RATE LIMIT note in loop().
const long PPG_POLL_MS         = 5;
unsigned long lastPpgPoll      = 0;

const int  HEART_MIN_BATCH     = 16;
const long HEART_MAX_WAIT_MS   = 50;
unsigned long lastHeartSend    = 0;
int16_t   heartBuf[HEART_BATCH_MAX * 2];   // interleaved filtered, derivative
int       heartCount = 0;

// Packet headers — see the protocol block at the top.
// ~15 fps. See the FRAME RATE CAP note in loop().
// ~5 fps, down from 15 (66 ms).
//
// The camera is essentially ALL of the bandwidth: 6404 bytes per frame against
// a handful for every sensor channel. At 15 fps that is ~96 kB/s, and MathWorks
// state plainly that serialport cannot sustain high-rate USB streaming — so
// MATLAB ran permanently behind and permanently discarded. Losing most packets
// is invisible on the LDR and temperature (slow scalars, you still see a
// current value) but destroys the PPG, which needs every sample.
//
// 200 ms = ~32 kB/s, a third of the load. Still 1-2 frames per blink for
// tracking. Raise it again once the PPG is confirmed working, and watch
// rx kB/s in MATLAB's [HEART] line to see how much headroom is left.
// *** THE CAMERA-vs-PPG TRADE-OFF KNOB. Change this one number only. ***
//
// A frame is 6404 bytes; every sensor channel together is a handful. So the
// camera IS the bandwidth, and MATLAB's serialport has a hard ceiling on how
// much it can drink (see the BULK READ note in Simplified_eye_V5.m). Exceed it
// and MATLAB falls permanently behind and discards — which barely shows on the
// LDR and temperature but destroys the PPG waveform.
//
//   ms    fps   kB/s   note
//   250   4.0   25.0   very safe
//   200   5.0   31.3   confirmed working 2026-08-26
//   167   6.0   37.4   very safe
//   125   8.0   50.0   likely safe  <-- set here: smoother, still ~half of what broke
//   100  10.0   62.5   risky
//    66  15.2   94.8   CONFIRMED BROKEN — this is what starved the PPG
//
// AFTER CHANGING THIS, watch the [HEART] line in MATLAB for ~30 s:
//   backlog stable and flush=0   -> headroom left, you may go lower still
//   backlog climbing or flush>0  -> too fast, go back up
// Those two numbers are the whole test; do not judge it by eye.
// V7: start value only. MATLAB sets it at run time with 0xF4 <ms> (the host
// now drains packets in bulk, so the old 15 fps limit is re-measured there).
long FRAME_MIN_MS = 125;
unsigned long lastFrameSend = 0;

const uint8_t HDR_FRAME[4]  = {255, 0, 255, 0};
const uint8_t HDR_LDR[4]    = {255, 0, 254, 0};
const uint8_t HDR_TEMP[4]   = {255, 0, 253, 0};
const uint8_t HDR_HEART[4]  = {255, 0, 252, 0};
const uint8_t HDR_STATUS[4] = {255, 0, 251, 0};

// --- CAPABILITY FLAGS — the whole point of V5's independence rework ---
// Each subsystem stands alone. No flag gates another, and nothing halts: a dead
// camera must not take the LDR, heart or temperature channels down with it, and
// missing I2C sensors must not stop eye tracking.
//
// The LDR (ADC on GPIO1) and the IR LEDs (GPIO2 output) have no failure mode to
// detect — a plain pin either reads or drives — so they get no flag and are
// always considered available.
bool haveCamera = false;

// --- V7: CAMERA ON ITS OWN CORE ---
// esp_camera_fb_get() blocks until a frame is ready (~40-80 ms). In the single
// loop that starved everything else: measured 2026-10-28, any frame rate above
// 8 fps dropped the heart stream from 100 to ~1.5 samples/s (the "15 fps
// CONFIRMED BROKEN" note). The camera now runs in cameraTask on core 0 and the
// sensors stay in loop() on core 1. Every packet write takes txMux, so the two
// can never interleave bytes inside a packet.
SemaphoreHandle_t txMux = NULL;
volatile bool camHostUp = false;
unsigned long lastRxMs = 0;          // last byte received from the host (keepalive)
#define TX_LOCK()   xSemaphoreTake(txMux, portMAX_DELAY)
#define TX_UNLOCK() xSemaphoreGive(txMux)
// NON-BLOCKING SENDS. A packet is written only if it fits in the TX buffer
// right now, otherwise skipped (heart samples just wait for the next batch).
// Measured 2026-10-28: when the host disconnected mid-frame, the camera
// task's write blocked while holding txMux, loop() then waited on txMux
// forever, nothing was sent or read again until a physical replug.
#define TX_ROOM(n)  (Serial.availableForWrite() >= (int)(n))
// havePpg / haveMlx are declared with the I2C block above.

const long STATUS_SEND_MS = 1000;   // 1 Hz subsystem heartbeat
unsigned long lastStatusSend = 0;

// Re-probe missing hardware at most this often. Lets a sensor be plugged in, a
// ribbon reseated, or a marginal rail settle, without resetting the board.
// Costs nothing once everything is up, because it is skipped entirely then.
const long RETRY_MS = 10000;
unsigned long lastRetry = 0;

// Status bitmask — must match decodeStatus() in Simplified_eye_V5.m
#define ST_CAMERA 0x01
#define ST_LDR    0x02
#define ST_HEART  0x04
#define ST_TEMP   0x08
#define ST_IRLED  0x10

bool irLedOn = false;   // mirrors the GPIO so the status byte can report it

// Spell out what the driver's error code actually means. "FAILED (0x106)" sent
// us checking PSRAM for an hour when PSRAM had nothing to do with it.
void explainCameraError(esp_err_t err) {
  Serial.println();
  Serial.printf("esp_camera_init FAILED (0x%x)\n", err);
  switch (err) {
    case 0x106:   // ESP_ERR_NOT_SUPPORTED
      Serial.println("  = \"Detected camera not supported\": the driver probed every known");
      Serial.println("    sensor address on SCCB (GPIO40=SDA, GPIO39=SCL) and NOTHING replied.");
      Serial.println("    The camera is not answering. This is NOT a PSRAM or pixel-format fault.");
      Serial.println("  -> Reseat the camera ribbon. It is the single most common cause.");
      Serial.println("  -> UNPLUG the MAX30102 and MLX90614 breakouts and retry. Two extra");
      Serial.println("     boards on 3V3 can sag the rail enough at boot that the OV2640's");
      Serial.println("     power-on reset never completes, and a camera still in reset is");
      Serial.println("     silent on SCCB. If it works unplugged, the fault is electrical.");
      Serial.println("  -> If it still fails unplugged, set ENABLE_I2C_SENSORS 0 at the top of");
      Serial.println("     this file and re-upload. That builds camera+LDR only. Working then");
      Serial.println("     would mean the fault IS in this sketch's added code — tell me.");
      Serial.println("  NOTE: the sketch KEEPS RUNNING. LDR, heart and temperature still");
      Serial.println("  stream, and the camera is retried every 10 s — reseat the ribbon and");
      Serial.println("  it will come back on its own, no reset needed.");
      break;
    case 0x101:   // ESP_ERR_NO_MEM
      Serial.println("  = out of memory. Check Tools>PSRAM = \"OPI PSRAM\".");
      break;
    case 0x105:   // ESP_ERR_NOT_FOUND
      Serial.println("  = camera not detected at all. Check the ribbon cable seating.");
      break;
    default:
      Serial.println("  Check the ribbon cable, PSRAM setting, and board selection.");
      break;
  }
}

void resetFilters() {
  dcW = 0.0f;
  lpfPrev = 0.0f;
  filtPrev = 0.0f;
  primed = false;
  settle = 0;
}

// Write a signed 16-bit value low byte first, matching the uint16 LDR field.
void writeInt16LE(int16_t v) {
  uint16_t u = (uint16_t)v;
  Serial.write((uint8_t)(u & 0xFF));
  Serial.write((uint8_t)((u >> 8) & 0xFF));
}

#if ENABLE_I2C_SENSORS
bool i2cPresent(TwoWire &bus, uint8_t addr) {
  bus.beginTransmission(addr);
  return bus.endTransmission() == 0;
}

// Bind one (bus object, pin pair) combination and report how many of our two
// sensors answer on it. Returns -1 if the bus could not even be brought up,
// which is how a port already owned by the camera shows up.
int probeCombo(TwoWire &bus, int sda, int scl, bool &sawMlx, bool &sawMax) {
  bus.end();
  if (!bus.begin(sda, scl)) return -1;    // port unavailable (SCCB owns it)
  bus.setClock(I2C_BUS_HZ);
  delay(40);
  sawMlx = i2cPresent(bus, MLX_ADDR);
  sawMax = i2cPresent(bus, MAX_ADDR);
  return (sawMlx ? 1 : 0) + (sawMax ? 1 : 0);
}
#endif

// Bring up the I2C sensors and work out which bus the MLX90614 is on.
// Prints a report — this all happens BEFORE streaming starts, so MATLAB's
// header hunt skips it harmlessly.
#if !ENABLE_I2C_SENSORS
void setupI2CSensors(bool announce) {
  if (announce) {
    Serial.println("I2C sensors COMPILED OUT (ENABLE_I2C_SENSORS 0) — camera + LDR only.");
  }
}
#else
// Re-entrant by design: retryMissingHardware() calls this again for whatever is
// still absent. Anything already working is left strictly alone — never torn
// down and re-inited — so a re-probe for the MLX90614 cannot interrupt a
// running PPG stream.
//
// `announce` is false on retries: a permanently absent sensor would otherwise
// print a NOT FOUND line every 10 s and bury everything else. Successes always
// print, because those are rare state changes worth seeing.
void setupI2CSensors(bool announce) {
  static bool     searched   = false;
  static TwoWire *sensorBus  = nullptr;
  static int      busSda = -1, busScl = -1;
  static bool     sawMlx = false, sawMax = false;

  // --- ONE-TIME BUS SEARCH ---------------------------------------------
  // BOTH sensors must share ONE bus. The ESP32-S3 has only two I2C
  // peripherals (SOC_I2C_NUM = 2) and the camera's SCCB permanently owns one
  // of them — its driver literally keeps a global called sccb_owns_i2c_port.
  // Arduino hardcodes Wire to bus 0 and Wire1 to bus 1, so asking for Wire AND
  // Wire1 AND the camera is three buses on a two-bus chip. One always loses.
  // An earlier version did exactly that, which is why a sensor on D2/D3 could
  // never be found while the camera was running.
  //
  // Which port SCCB takes is not knowable up front, so rather than guess, try
  // both bus objects against both pin pairs and keep whichever actually
  // answers. bus.begin() returning false is precisely how a port already owned
  // by the camera announces itself.
  if (!searched) {
    searched = true;
    if (announce) Serial.println("I2C bus search (both peripherals, one pin pair):");
    TwoWire *buses[2]   = { &Wire, &Wire1 };
    const char *bn[2]   = { "Wire(bus0)", "Wire1(bus1)" };
    int pins[1][2]      = { { I2C_SDA, I2C_SCL } };
    const char *pn[1]   = { "D4/D5 (GPIO5/6)" };

    int best = 0;
    for (int b = 0; b < 2; b++) {
      for (int p = 0; p < 1; p++) {
        bool m = false, x = false;
        int n = probeCombo(*buses[b], pins[p][0], pins[p][1], m, x);
        if (announce) {
          Serial.printf("  probe %-11s %-16s : ", bn[b], pn[p]);
          if (n < 0) {
            Serial.println("bus unavailable (camera owns this port)");
          } else if (n == 0) {
            // Sweep every 7-bit address before declaring the combination dead.
            // "Our two addresses did not answer" and "nothing on this bus at
            // all" need different fixes: the first means a wrong/reprogrammed
            // address, the second means wiring, power or pull-ups.
            int other = 0;
            for (uint8_t a = 1; a < 127; a++) {
              if (a == MLX_ADDR || a == MAX_ADDR) continue;
              if (i2cPresent(*buses[b], a)) {
                if (other == 0) Serial.print("no known device, but found ");
                Serial.printf("0x%02X ", a);
                other++;
              }
            }
            Serial.println(other ? "" : "nothing at all");
          } else {
            if (m) Serial.print("0x5A MLX90614  ");
            if (x) Serial.print("0x57 MAX30102");
            Serial.println();
          }
        }
        if (n > best) {
          best = n; sensorBus = buses[b];
          busSda = pins[p][0]; busScl = pins[p][1];
          sawMlx = m; sawMax = x;
        }
      }
    }

    // RELEASE THE LOSING PERIPHERAL. The loop above begins BOTH bus objects
    // while probing, and each one it leaves initialised is a port consumed.
    // Holding both means the camera has NO free port on a later init attempt,
    // at which point SCCB attaches to whichever bus already exists and its
    // matching deinit rips devices off OUR bus — which is precisely how the
    // temperature reading died a few seconds after a good start.
    for (int b = 0; b < 2; b++) {
      if (buses[b] != sensorBus) buses[b]->end();
    }

    if (sensorBus == nullptr) {
      if (announce) Serial.println("  no I2C sensors found on any bus/pin combination");
      // Allow the next retry to search again, so a sensor plugged in after boot
      // is still picked up. Once a bus HAS been chosen the search never repeats
      // — re-running it would tear down a bus that is working.
      searched = false;
      return;
    }
    // Re-bind the winning combination — the search left the bus on whatever it
    // tried last.
    probeCombo(*sensorBus, busSda, busScl, sawMlx, sawMax);
    if (announce) {
      Serial.printf("  using SDA=GPIO%d SCL=GPIO%d at %d Hz\n", busSda, busScl, I2C_BUS_HZ);
    }
  }

  if (sensorBus == nullptr) return;

  if (!havePpg && sawMax) {
    // I2C_BUS_HZ, never I2C_SPEED_FAST — see the constant's comment. This call
    // internally does _i2cPort->setClock(), so passing 400 kHz here silently
    // reconfigures the WHOLE bus and breaks the MLX90614 sharing it.
    havePpg = ppg.begin(*sensorBus, I2C_BUS_HZ);
    if (havePpg) {
      ppg.setup(LED_BRIGHTNESS, SAMPLE_AVERAGE, LED_MODE,
                SAMPLE_RATE, PULSE_WIDTH, ADC_RANGE);
      // BOTH LEDs stay lit. Zeroing Red to save current is safe on a MAX30105
      // but NOT on a MAX30102, where the channels are commonly swapped against
      // what this library assumes — getFIFOIR() then reads the slot driven by
      // LED1_PA, and zeroing it yields an endless "0,0" with a finger on the
      // sensor. Costs a few mA; works on either part.
      ppg.setPulseAmplitudeRed(LED_BRIGHTNESS);
      ppg.setPulseAmplitudeIR(LED_BRIGHTNESS);
      resetFilters();
      Serial.println("MAX30102 OK");
    } else if (announce) {
      Serial.println("MAX30102 answered the scan but would not init");
    }
  }

  if (!haveMlx && sawMlx) {
    haveMlx = mlx.begin(MLX_ADDR, sensorBus);
    if (haveMlx) Serial.println("MLX90614 OK");
    else if (announce) Serial.println("MLX90614 answered the scan but would not init");
  }

  // Re-scan on retries so a sensor plugged in later is picked up. Cheap: two
  // address probes on a bus that is already up.
  if (!havePpg || !haveMlx) {
    sawMlx = sawMlx || i2cPresent(*sensorBus, MLX_ADDR);
    sawMax = sawMax || i2cPresent(*sensorBus, MAX_ADDR);
  }

  // A missing sensor is NOT fatal to anything else. Each channel simply stops
  // emitting its packets; MATLAB shows it as absent and everything else runs.
}
#endif  // ENABLE_I2C_SENSORS

// Bring the camera up. Returns whether it worked; NEVER halts.
//
// Also used by retryMissingHardware(), so a reseated ribbon recovers without a
// board reset. Safe to call repeatedly: on failure esp_camera_init cleans up
// after itself (confirmed by disassembly — it calls esp_camera_deinit on the
// probe-failure path), so this must NOT deinit again. Doing exactly that was
// what turned "an error message" into "a blank Serial Monitor": the second
// deinit double-freed cam_obj, corrupted the heap and panicked the chip before
// any output could flush.
// `announce` is false on retries — the 0x106 explanation is a dozen lines and
// would bury the Serial Monitor if it reprinted every 10 s.
bool initCamera(bool announce) {
  // ZERO-INITIALISED. V4 declared this bare, which left jpeg_quality and
  // sccb_i2c_port holding whatever was on the stack — and esp_camera_init
  // memcpy's all 104 bytes into its saved config. V4 got away with it because
  // the garbage happened to be benign, which is not the same as being correct.
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_GRAYSCALE;
  config.frame_size = FRAMESIZE_240X240;
  // V7: two buffers. With one, the sensor cannot start the next frame until
  // the last is handed back, so it delivered every other frame (12.5 fps max).
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  // PSRAM is required for FB_IN_PSRAM. No longer fatal to the whole sketch —
  // only to the camera. Everything else runs fine without it.
  if (!psramFound()) {
    if (announce) {
      Serial.println("PSRAM NOT FOUND -> set Tools>PSRAM to \"OPI PSRAM\" and re-upload.");
      Serial.println("  Camera disabled. LDR / heart / temperature are unaffected.");
    }
    return false;
  }

  // Two attempts with a pause: if the 3V3 rail sags at boot the OV2640's
  // power-on reset can finish late and the second attempt then succeeds.
  esp_err_t err = ESP_FAIL;
  for (int attempt = 1; attempt <= 2; attempt++) {
    err = esp_camera_init(&config);
    if (err == ESP_OK) break;
    if (attempt == 1) delay(300);   // no deinit here — see the note above
  }
  if (err != ESP_OK) {
    if (announce) explainCameraError(err);
    return false;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s == NULL) {
    if (announce) Serial.println("esp_camera_sensor_get returned NULL — camera disabled.");
    return false;
  }

  s->set_vflip(s, 1);
  s->set_hmirror(s, 1);

#if CAMERA_AUTO_EXPOSURE
  // Diagnostic only. See CAMERA_AUTO_EXPOSURE above.
  s->set_gain_ctrl(s, 1);                // AGC on
  s->set_exposure_ctrl(s, 1);            // AEC on
  s->set_aec2(s, 1);                     // AEC DSP on
  Serial.println("CAMERA: AUTO exposure/gain (DIAGNOSTIC MODE — brightness will hunt)");
#else
  // --- GAIN LOCK (disable auto BEFORE writing manual value) ---
  s->set_gain_ctrl(s, 0);              // AGC off
  s->set_agc_gain(s, FIXED_AGC_GAIN);  // fixed manual gain

  // --- EXPOSURE LOCK (disable autos BEFORE writing manual value) ---
  s->set_exposure_ctrl(s, 0);            // AEC off
  s->set_aec2(s, 0);                     // AEC DSP off
  s->set_aec_value(s, FIXED_AEC_VALUE);  // fixed manual exposure
#endif
  return true;
}

// Re-probe whatever is currently missing. Called from loop() at most every
// RETRY_MS, and only while something is actually absent — when everything is up
// this costs a single comparison per pass.
//
// THE CAMERA IS DELIBERATELY NOT RETRIED BY DEFAULT. esp_camera_init() is not a
// side-effect-free probe: it runs SCCB_Init(), which creates or attaches to an
// I2C master bus, and on failure esp_camera_deinit() runs SCCB_Deinit(), which
// removes devices from that bus. Doing that every 10 s next to a working
// MLX90614 killed the temperature channel a few seconds into every session —
// trading a live sensor for a speculative camera recovery, which is a bad deal.
// A board reset costs nothing; a silently dying sensor costs an experiment.
//
// Set CAMERA_HOT_RETRY to 1 only while actively wiggling a camera ribbon, and
// expect the I2C sensors to drop out while you do.
#define CAMERA_HOT_RETRY 0

void retryMissingHardware() {
  if (haveCamera && havePpg && haveMlx) return;
  if (millis() - lastRetry < RETRY_MS) return;
  lastRetry = millis();

#if CAMERA_HOT_RETRY
  if (!haveCamera) {
    haveCamera = initCamera(false);
    if (haveCamera) Serial.println("Camera recovered — frames resuming.");
  }
#endif
  if (!havePpg || !haveMlx) setupI2CSensors(false);   // quiet on retries
}

// One-screen subsystem table. Printed after the USB CDC settling delay and
// before the binary stream starts, so it is readable in the Serial Monitor.
// MATLAB's header hunt skips plain text harmlessly.
// Measure what the PPG actually produces, instead of inferring it from a flat
// plot. Runs the REAL DSP over a few seconds and reports the two numbers that
// decide everything downstream:
//
//   DC level  — where the raw channel sits. Near 262143 means saturated (a
//               constant, which the DC blocker turns into exactly zero). Near
//               zero means no light reaching the photodiode.
//   AC p-p    — the pulsatile component the filter extracts. THIS is what gets
//               plotted, and if it is a handful of counts no display setting
//               will rescue it: the operating point is wrong.
//
// Text is safe here because this runs before the binary stream starts.
#if ENABLE_I2C_SENSORS && PPG_CENSUS
void ppgCensus() {
  Serial.println();
  Serial.println("    --- PPG CENSUS: put a finger on the sensor NOW ---");
  for (int i = 3; i > 0; i--) { Serial.printf("      starting in %d...\n", i); delay(1000); }
  Serial.println("      measuring for 5 s, hold still");

  uint32_t dcMin = 0xFFFFFFFF, dcMax = 0;
  float    acMin = 1e9f, acMax = -1e9f;
  uint32_t n = 0;

  // Fresh filter state so the census is not polluted by whatever came before.
  resetFilters();
  unsigned long t0 = millis();
  while (millis() - t0 < 5000) {
    ppg.check();
    while (ppg.available()) {
      uint32_t red = ppg.getFIFORed();
      uint32_t irR = ppg.getFIFOIR();
      ppg.nextSample();
      long v = (chan == CH_RED) ? (long)red : (long)irR;

      if (!primed) {
        dcW = (float)v / (1.0f - DC_ALPHA);
        lpfPrev = 0.0f; filtPrev = 0.0f; primed = true; settle = SETTLE_SAMPLES;
      }
      float wNew = (float)v + DC_ALPHA * dcW;
      float ac   = wNew - dcW;
      dcW = wNew;
      float filt = LPF_ALPHA * ac + (1.0f - LPF_ALPHA) * lpfPrev;
      lpfPrev = filt;

      // Skip the settle window, or the priming step dominates acMax and the
      // p-p reads in the hundreds of thousands on every run.
      if (settle > 0) { settle--; continue; }

      if ((uint32_t)v < dcMin) dcMin = (uint32_t)v;
      if ((uint32_t)v > dcMax) dcMax = (uint32_t)v;
      if (filt < acMin) acMin = filt;
      if (filt > acMax) acMax = filt;
      n++;
    }
  }

  Serial.printf ("      samples   : %lu  (expect ~1000 at 200 Hz)\n", (unsigned long)n);
  if (n == 0) {
    Serial.println("      *** NO SAMPLES — the FIFO never advanced. ***");
    return;
  }
  Serial.printf ("      DC range  : %lu .. %lu\n", (unsigned long)dcMin, (unsigned long)dcMax);
  long acpp = (long)(acMax - acMin);
  Serial.printf ("      AC p-p    : %ld  <-- this is what gets plotted\n", acpp);

  if (dcMax > 260000) {
    Serial.println("      VERDICT: SATURATED. Raise ADC_RANGE or lower LED_BRIGHTNESS.");
  } else if (dcMax < 1000) {
    Serial.println("      VERDICT: almost no light. Check the finger is ON the window.");
  } else if (acpp < 20) {
    Serial.println("      VERDICT: DC is fine but there is almost NO pulsatile signal.");
    Serial.println("        Optical contact is the usual cause — rest the finger flat and");
    Serial.println("        still, do not press. If it stays this low, LOWER ADC_RANGE");
    Serial.println("        (32768 -> 16384 -> 8192) to regain sensitivity.");
  } else {
    Serial.printf ("      VERDICT: healthy. A +/-%ld trace should be clearly visible.\n", acpp / 2);
  }
  resetFilters();
}
#endif

void printBootReport() {
  Serial.println();
  Serial.println("======================================================");
  Serial.println(" Simplified_eye_V5 — subsystem status");
  Serial.println("------------------------------------------------------");
  Serial.printf ("  Camera (OV2640)   : %s\n", haveCamera ? "OK" : "NOT AVAILABLE");
  if (haveCamera) {
    // The single number that separates "camera broken" from "exposure too low
    // for this lighting" — the two look identical on screen.
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb) {
      uint32_t sum = 0;
      for (int y = START_Y; y < START_Y + ROI_SIZE; y++)
        for (int x = 0; x < ROI_SIZE; x++) sum += fb->buf[y * IMG_W + START_X + x];
      esp_camera_fb_return(fb);
      uint32_t mean = sum / (ROI_SIZE * ROI_SIZE);
      Serial.printf ("    ROI mean bright.: %lu / 255  %s\n", (unsigned long)mean,
                     mean < 8 ? "<-- BLACK. IR LEDs off, or raise FIXED_AEC_VALUE"
                              : (mean < 30 ? "(dim)" : "(usable)"));
    }
  }
  // Printed FROM THE MACROS, never hardcoded. These lines previously read
  // "LDR (GPIO1/D0)" and "IR LEDs (GPIO2/D1)" long after both pins had moved,
  // which is exactly the sort of stale label that costs an hour.
  Serial.printf ("  LDR (GPIO%d)       : OK (no failure mode to detect)\n", LDR_PIN);
  Serial.printf ("  IR LEDs (GPIO%d)   : OK (no failure mode to detect)\n", LED_GPIO_NUM);
#if ENABLE_I2C_SENSORS
  Serial.printf ("  Heart (MAX30102)  : %s\n", havePpg ? "OK" : "NOT AVAILABLE");
  if (havePpg) {
    // Raw IR straight off the FIFO, no filtering, no threshold. The same
    // number MAX30102_RawCheck.ino prints, so the two are directly comparable
    // and "it works standalone but not here" becomes a checkable claim.
    // BOTH channels, because which one carries the signal is decided at run
    // time by the channel auto-lock. Printing only IR would look dead on a
    // board whose channels are swapped.
    uint32_t redNow = 0, irNow = 0;
    unsigned long t0 = millis();
    while (millis() - t0 < 300) {         // let the FIFO fill a little
      ppg.check();
      while (ppg.available()) {
        redNow = ppg.getFIFORed();
        irNow  = ppg.getFIFOIR();
        ppg.nextSample();
      }
    }
    Serial.printf ("    raw (no finger)   : red=%lu  ir=%lu\n",
                   (unsigned long)redNow, (unsigned long)irNow);
    // 2000 was the right alarm level back when ADC_RANGE was 4096. At 16384 the
    // same light reads about 4x SMALLER, so a perfectly healthy resting level of
    // ~450-550 tripped it and the report cried "LEDs not lighting" on a sensor
    // that was working. Scaled to match the range actually in use.
    if (redNow < 150 && irNow < 150) {
      Serial.println("      *** BOTH channels dark — LEDs not lighting ***");
    }
    Serial.println("    no presence gate  : the DSP runs on every sample and");
    Serial.println("                        publishes unconditionally. A flat trace");
    Serial.println("                        now means a flat SIGNAL, not a closed gate.");
#if PPG_CENSUS
    ppgCensus();
#else
    Serial.println("    census OFF (PPG_CENSUS 0) — set to 1 to re-measure AC p-p");
#endif
  }
  Serial.printf ("  Temp  (MLX90614)  : %s\n", haveMlx ? "OK" : "NOT AVAILABLE");
#else
  Serial.println("  Heart / Temp      : COMPILED OUT (ENABLE_I2C_SENSORS 0)");
#endif
  Serial.println("------------------------------------------------------");
  Serial.println(" Every subsystem is independent. A missing I2C sensor is");
  Serial.println(" retried every 10 s — plug it in and it joins the stream.");
  Serial.println(" The CAMERA is not retried: its init disturbs the shared");
  Serial.println(" I2C bus, so recovering it needs a board reset.");
  Serial.println("======================================================");
  Serial.println();
}

void setup() {
  // V7: room for two whole frames, so every packet can be written without
  // waiting (see TX_ROOM) -- must be set before begin().
  Serial.setTxBufferSize(16384);
  Serial.begin(2000000);

  // TX timeout stays at the 100 ms default. It was briefly set to 0 to stop the
  // board wedging when MATLAB closed the port, and that DID fix the wedge — but
  // at 0 the tx lock fails instantly under backpressure, write() returns 0 and
  // silently DROPS the bytes. Mid-frame that produces a torn image and, worse,
  // leaves the byte stream misaligned so the small LDR and heart packets are
  // eaten by the host's resync while the sparse temperature ones still slip
  // through. That was exactly the reported symptom.
  //
  // The wedge is solved properly below instead: loop() refuses to queue
  // anything at all while no host is attached, so nothing is ever half-written.

  // The ESP32S3 talks over native USB CDC. Without this pause the host has not
  // finished enumerating the port when the first prints happen, and they are
  // dropped — which is why a failing boot looked like a blank Serial Monitor.
  delay(2500);

  // --- LDR: no failure mode, so it comes up first and unconditionally ---
  // 10-bit to match the Uno the thresholds were calibrated on. Without this
  // the ESP32 defaults to 12-bit (0-4095) and every MATLAB threshold is wrong
  // by 4x.
  analogReadResolution(10);

  // Pin the attenuation explicitly. Without this the pin inherits whatever the
  // core's default happens to be, which has changed between Arduino-ESP32 core
  // versions (2.x ADC_11db, 3.x ADC_12db). Attenuation sets the ADC's
  // full-scale voltage, so a different value remaps every reading NON-linearly.
  analogSetPinAttenuation(LDR_PIN, ADC_11db);   // full scale ~3.1 V

  // Prime the smoothing window with a real sample so the first value sent is
  // already an average.
  int first = analogRead(LDR_PIN);
  for (int i = 0; i < SMOOTH_N; i++) ldrBuf[i] = first;
  ldrSum = (long)first * SMOOTH_N;

  // --- IR LEDs: also no failure mode. Start OFF; MATLAB sends 0xF1/0xF0 ---
  pinMode(LED_GPIO_NUM, OUTPUT);
  digitalWrite(LED_GPIO_NUM, LOW);
  irLedOn = false;

  // --- I2C SENSORS FIRST, CAMERA SECOND. The order is load-bearing. ---
  //
  // esp_camera_init() is not a passive probe: it runs SCCB_Init(), which calls
  // i2c_new_master_bus(), and on failure esp_camera_deinit() runs SCCB_Deinit()
  // -> i2c_master_bus_rm_device / i2c_del_master_bus. With no camera attached
  // the retry below runs that create/destroy cycle TWICE, which is exactly the
  //     E i2c.common: acquire bus failed
  //     E i2c.common: i2c_release_bus_handle(249): Bus not freed entirely
  // pair seen in the boot report — the I2C subsystem left in a state where a
  // subsequent Wire.begin() succeeds but no device on it ever ACKs.
  //
  // Claiming our bus first means SCCB takes whichever peripheral is left and
  // its churn cannot land on a bus we are already using. It also matches the
  // hardware reality: SCCB auto-selects a free port, so going second costs it
  // nothing.
  setupI2CSensors(true);

#if ENABLE_CAMERA
  haveCamera = initCamera(true);
#else
  // Not merely "skip the frame send" — do not call esp_camera_init() AT ALL, so
  // SCCB never touches an I2C peripheral. This is the clean build for deciding
  // whether the camera path is what disturbs the sensors.
  haveCamera = false;
  Serial.println("CAMERA COMPILED OUT (ENABLE_CAMERA 0) — esp_camera_init not called.");
#endif

  printBootReport();

  txMux = xSemaphoreCreateMutex();
#if ENABLE_CAMERA
  xTaskCreatePinnedToCore(cameraTask, "camera", 4096, NULL, 1, NULL, 0);   // loop() is on core 1
#endif
}

void loop() {
  // HOST PRESENCE — gates the EXPENSIVE writes only, never all of them.
  //
  // This used to `return` outright when !Serial, and that deadlocked the link.
  // HWCDC::operator bool() calls isCDC_Connected(), which only flips `connected`
  // true once the host actually CLOCKS DATA OUT — so refusing to write anything
  // meant there was never anything to clock, so it never reconnected, so we
  // never wrote. The board sat mute until a physical reset, which is exactly the
  // "7 runs out of 10 will not start" behaviour.
  //
  // The 1 Hz status packet is now always allowed through. Five bytes is enough
  // for the host to clock and flip the flag, it costs at most one TX timeout per
  // second against a dead host, and it doubles as the sync beacon MATLAB waits
  // for. Only the 6.4 kB frame is gated below.
  // V7: OR any byte from the host in the last 3 s (MATLAB V7 sends a 0xF7
  // keepalive twice a second). Measured 2026-10-28: after a reset or a host
  // reconnect the HWCDC flag below sometimes stayed false even while the host
  // was reading the status/LDR packets -- frames and heart were then withheld
  // until a physical replug ("re-running needs the glasses unplugged").
  bool hostUp = (bool)Serial || (millis() - lastRxMs < 3000);

  // Check for LED control command from MATLAB (0xF1=ON, 0xF0=OFF)
  if (Serial.available()) {
    uint8_t cmd = Serial.read();
    lastRxMs = millis();               // any byte (0xF7 = keepalive, no-op) = host present
    // Handled unconditionally, before any capability check, so the IR LEDs work
    // even when the camera and both I2C sensors are absent.
    if      (cmd == 0xF1) { digitalWrite(LED_GPIO_NUM, HIGH); irLedOn = true;  }
    else if (cmd == 0xF0) { digitalWrite(LED_GPIO_NUM, LOW);  irLedOn = false; }
    // V7 run-time camera settings (argument bytes follow the command):
    //   0xF4 ms              frame interval, 0..250 ms (0 = as fast as the camera)
    //   0xF5 hi lo           exposure (aec_value) 0..1200
    //   0xF6 g               analog gain 0..30
    else if (cmd >= 0xF4 && cmd <= 0xF6) {
      int need = (cmd == 0xF5) ? 2 : 1;
      unsigned long t0 = millis();
      while (Serial.available() < need && millis() - t0 < 50) delay(1);
      if (Serial.available() >= need) {
        int a = Serial.read();
        int b = (need == 2) ? Serial.read() : 0;
#if ENABLE_CAMERA
        sensor_t *cs = esp_camera_sensor_get();
#endif
        if (cmd == 0xF4) FRAME_MIN_MS = constrain(a, 0, 250);
#if ENABLE_CAMERA
        else if (cmd == 0xF5 && cs) cs->set_aec_value(cs, constrain(a * 256 + b, 0, 1200));
        else if (cmd == 0xF6 && cs) cs->set_agc_gain(cs, constrain(a, 0, 30));
#endif
      }
    }
  }

  // Re-probe anything missing. No-op once everything is up.
  retryMissingHardware();

  // --- LDR SAMPLE (from V4, unchanged) ---
  // Sampling runs on its own 20 ms cadence rather than once per camera loop.
  // Tied to the frame rate, the 10-sample average took ~0.7 s to settle and
  // stalled completely whenever frames stalled.
  if (millis() - lastLdrSample >= LDR_SAMPLE_MS) {
    lastLdrSample = millis();
    ldrSum -= ldrBuf[ldrIdx];
    ldrBuf[ldrIdx] = analogRead(LDR_PIN);
    ldrSum += ldrBuf[ldrIdx];
    ldrIdx = (ldrIdx + 1) % SMOOTH_N;
  }

  // --- HEART: DRAIN THE FIFO INTO A BATCH (NEW IN V5) ---
  // Draining in a loop rather than one sample per pass stops the host seeing
  // bursts followed by gaps when a frame send slows this down. The MAX30102's
  // 32-sample FIFO holds 320 ms at 100 Hz, comfortably more than one frame.
  // NOTE: heartCount is deliberately NOT cleared here. It accumulates across
  // loop passes and is cleared only when a batch is actually SENT, below.
  // Clearing it every pass is what fragmented the stream: once the frame cap
  // let loop() return early, this ran hundreds of times a second, found 0-1
  // samples each time, and emitted ~200 single-sample packets per second. The
  // host processes one packet per iteration, so it could not keep up and the
  // backlog guard threw the lot away — 133 samples received out of ~20000 sent.
#if ENABLE_I2C_SENSORS
  // POLL RATE LIMIT. ppg.check() is not free: each call is an I2C transaction
  // reading the FIFO pointers, ~0.5 ms at 100 kHz. Once the frame cap let
  // loop() return early it began spinning hundreds of times a second, so this
  // was burning a fifth of the bus asking "anything new?" — on a bus the
  // MLX90614 also has to share. Polling at 5 ms is still 2.5x the 200 Hz
  // sample rate, so nothing is missed.
  if (havePpg && millis() - lastPpgPoll >= PPG_POLL_MS) {
    lastPpgPoll = millis();
    ppg.check();
    while (ppg.available() && heartCount < HEART_BATCH_MAX) {
      uint32_t red = ppg.getFIFORed();
      uint32_t irRaw = ppg.getFIFOIR();
      ppg.nextSample();

      // Peak trackers for the slow channel decision below.
      if (red   > chanMaxRed) chanMaxRed = red;
      if (irRaw > chanMaxIr)  chanMaxIr  = irRaw;

      long ir = (chan == CH_RED) ? (long)red : (long)irRaw;

      int16_t outFilt, outDeriv;
      {
        // Prime on the first sample so the DC blocker starts settled instead of
        // ramping from zero over the first second.
        if (!primed) {
          dcW = (float)ir / (1.0f - DC_ALPHA);
          lpfPrev = 0.0f;
          filtPrev = 0.0f;
          primed = true;
          settle = SETTLE_SAMPLES;
        }

        float wNew = (float)ir + DC_ALPHA * dcW;     // DC removal
        float ac   = wNew - dcW;
        dcW = wNew;

        float filt = LPF_ALPHA * ac + (1.0f - LPF_ALPHA) * lpfPrev;   // smoothing
        lpfPrev = filt;

        float deriv = (filt - filtPrev) * (float)DERIV_GAIN;          // first difference
        filtPrev = filt;

        // Clamp before the int16 cast. A saturating spike would otherwise wrap
        // and draw a full-scale line in the opposite direction.
        if (filt  >  32767.0f) filt  =  32767.0f;
        if (filt  < -32768.0f) filt  = -32768.0f;
        if (deriv >  32767.0f) deriv =  32767.0f;
        if (deriv < -32768.0f) deriv = -32768.0f;
        outFilt  = (int16_t)filt;
        outDeriv = (int16_t)deriv;

#if HEART_SEND_RAW
        // DIAGNOSTIC OVERRIDE — see HEART_SEND_RAW at the top of the file.
        // Publishes the RAW counts instead of the filter output, so the host
        // plots what the sensor actually reports. >>3 scales the 18-bit range
        // into int16. The derivative channel carries the OTHER colour, so both
        // are visible at once and a channel swap is obvious.
        //
        // Placed BEFORE the settle gate on purpose: it used to sit after it, so
        // raw bypassed the blanking and filtered did not. That made the two
        // modes non-comparable — "raw works, filtered doesn't" could have meant
        // a stuck settle rather than a DSP fault. Now the ONLY difference
        // between them is the filter itself.
        outFilt  = (int16_t)(((chan == CH_RED) ? red : irRaw) >> 3);
        outDeriv = (int16_t)(((chan == CH_RED) ? irRaw : red) >> 3);
#endif

        // Filter state has advanced; the arrival step is simply not published.
        if (settle > 0) { settle--; outFilt = 0; outDeriv = 0; }
      }

      // heartCount now persists across passes, so the cap must be respected
      // here too or a slow host could walk past the end of heartBuf.
      if (heartCount < HEART_BATCH_MAX) {
        heartBuf[heartCount * 2]     = outFilt;
        heartBuf[heartCount * 2 + 1] = outDeriv;
        heartCount++;
      }
    }
  }
#endif  // ENABLE_I2C_SENSORS

  // --- SLOW CHANNEL RE-DECISION ---
  // Switch ONLY when one side is decisively brighter. A bare
  // (chanMaxRed > chanMaxIr) here was a latch-up bug: the two channels on this
  // sensor read within ~15% of each other, so noise flipped the comparison
  // every window. Each flip called resetFilters(), the next sample re-primed
  // and armed SETTLE_SAMPLES of blanking, and with the window and the blanking
  // both 2 s the next flip arrived exactly as the blanking expired. The output
  // was pinned at zero forever — a dead trace from a working sensor.
  //
  // CHAN_SWITCH_MARGIN makes a swap require real evidence, not a coin toss.
#if ENABLE_I2C_SENSORS
  if (havePpg && millis() - lastChanEval >= CHAN_EVAL_MS) {
    lastChanEval = millis();
    uint8_t want = chan;
    if (chanMaxRed > chanMaxIr * CHAN_SWITCH_MARGIN)      want = CH_RED;
    else if (chanMaxIr > chanMaxRed * CHAN_SWITCH_MARGIN) want = CH_IR;
    if (want != chan) {
      chan = want;
      resetFilters();
    }
    chanMaxRed = 0;
    chanMaxIr  = 0;
  }
#endif

  // --- SENDS: all before the frame, never during it. A packet must not split.
  if (millis() - lastLdrSend >= LDR_SEND_MS) {
    lastLdrSend = millis();
    uint16_t ldrVal = (uint16_t)(ldrSum / SMOOTH_N);
    TX_LOCK();
    if (TX_ROOM(6)) {
    Serial.write(HDR_LDR, 4);
    Serial.write((uint8_t)(ldrVal & 0xFF));         // low byte first
    Serial.write((uint8_t)((ldrVal >> 8) & 0xFF));  // then high byte
    }
    TX_UNLOCK();
  }

  // Send a BATCH, not a dribble. Either enough samples have accumulated or
  // enough time has passed — whichever comes first, so the trace never stalls
  // waiting for a batch to fill. ~20 packets/s instead of ~200, which is what
  // the host can actually parse alongside the frames.
  if (hostUp && (heartCount >= HEART_MIN_BATCH ||
      (heartCount > 0 && millis() - lastHeartSend >= HEART_MAX_WAIT_MS))) {
    TX_LOCK();
    if (TX_ROOM(5 + 4 * heartCount)) {
      lastHeartSend = millis();
      Serial.write(HDR_HEART, 4);
      Serial.write((uint8_t)heartCount);
      for (int i = 0; i < heartCount * 2; i++) writeInt16LE(heartBuf[i]);
      heartCount = 0;
    }
    TX_UNLOCK();
  }

#if ENABLE_I2C_SENSORS
  if (haveMlx && millis() - lastTempSend >= TEMP_SEND_MS) {
    lastTempSend = millis();
    float ambient = mlx.readAmbientTempC();
    float object  = mlx.readObjectTempC();
    // Drop the sample rather than send NaN. Hundredths of a degree keeps the
    // sensor's whole -70..380 C range inside int16 with room to spare.
    if (!isnan(ambient) && !isnan(object)) {
      TX_LOCK();
      if (TX_ROOM(8)) {
        Serial.write(HDR_TEMP, 4);
        writeInt16LE((int16_t)lroundf(ambient * 100.0f));
        writeInt16LE((int16_t)lroundf(object  * 100.0f));
      }
      TX_UNLOCK();
    }
  }
#endif  // ENABLE_I2C_SENSORS

  // --- STATUS HEARTBEAT (NEW IN V5) ---
  // Tells MATLAB which subsystems are alive instead of leaving it to infer from
  // silence, and — the part that matters — guarantees the link always carries
  // traffic. Without it a board with no camera would emit nothing MATLAB could
  // sync on if the I2C sensors were also absent, and the host would sit at
  // "waiting for sync" forever with perfectly healthy hardware.
  if (millis() - lastStatusSend >= STATUS_SEND_MS) {
    lastStatusSend = millis();
    uint8_t st = ST_LDR;                      // the LDR cannot fail; always set
    if (haveCamera) st |= ST_CAMERA;
    if (havePpg)    st |= ST_HEART;
    if (haveMlx)    st |= ST_TEMP;
    if (irLedOn)    st |= ST_IRLED;
    TX_LOCK();
    if (TX_ROOM(5)) { Serial.write(HDR_STATUS, 4); Serial.write(st); }
    TX_UNLOCK();
  }

  // --- CAMERA FRAME ---
  // Guarded, and it must stay the LAST thing in the loop: everything above has
  // already been sent, so a missing or stalling camera cannot delay or suppress
  // any other channel.
  //
  // Commenting this block out by hand is what proved the camera and the I2C
  // sensors cannot currently run together — see ENABLE_CAMERA at the top. The
  // switch does the same job without editing code, so the two configurations
  // stay one #define apart instead of diverging.
  // Frames are sent by cameraTask (core 0); this loop only tells it whether
  // a host is there and stays free for the sensors.
  camHostUp = hostUp;
  delay(1);
}

#if ENABLE_CAMERA
void cameraTask(void *) {
  for (;;) {
    // No host / no camera: skip the expensive frame entirely. The small
    // packets in loop() still go out, which lets the CDC reconnect on its own.
    if (!camHostUp || !haveCamera || millis() - lastFrameSend < (unsigned long)FRAME_MIN_MS) {
      vTaskDelay(1);
      continue;
    }
    lastFrameSend = millis();
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) { vTaskDelay(1); continue; }
    TX_LOCK();
    if (TX_ROOM(4 + ROI_SIZE * ROI_SIZE)) {                   // whole frame fits, or skip it
      Serial.write(HDR_FRAME, 4);                             // header for MATLAB sync
      for (int y = START_Y; y < START_Y + ROI_SIZE; y++) {    // the 80x80 eye-box, row by row
        Serial.write(&fb->buf[y * IMG_W + START_X], ROI_SIZE);
      }
    }
    TX_UNLOCK();
    esp_camera_fb_return(fb);
  }
}
#endif

