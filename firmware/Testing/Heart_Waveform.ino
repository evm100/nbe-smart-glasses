/*
 * Heart_Waveform.ino
 * Created: 2026-08-11
 * Updated: 2026-08-19 — auto channel selection, stronger LED drive, and a
 *                       "# " diagnostic channel so a flat trace is explained
 *                       instead of just observed.
 *
 * IR photoplethysmogram (PPG) acquisition for the MAX30102 / MAX30105.
 * Streams a DC-removed IR waveform and its first derivative to the host.
 *
 * ---------------------------------------------------------------------------
 * SERIAL CONTRACT — must match Heart_Waveform_1.m exactly
 *
 *   115200 baud, ASCII, one sample per line, LF terminated:
 *
 *       <filtered>,<derivative>\n          e.g.  "-412,1730"
 *
 *   Two integers, comma separated, nothing else on the line. The host
 *   NEGATES both values before plotting — that is deliberate and matches the
 *   Python. In a PPG the IR reading DROPS as blood volume rises, so the raw
 *   AC signal is upside down; the host's negation puts the systolic peak the
 *   right way up. Do not pre-invert here or it will be flipped twice.
 *
 *   Anything else is prefixed "# ". Both hosts reject any line that is not two
 *   comma-separated numbers, so these ride alongside the samples harmlessly;
 *   Heart_Waveform_1.m echoes them to the MATLAB console (the Python printed
 *   every rejected line too). Never print a bare line that starts with digits.
 *
 * ---------------------------------------------------------------------------
 * LIBRARY: "SparkFun MAX3010x Pulse and Proximity Sensor Library"
 *   Arduino IDE -> Tools -> Manage Libraries -> search MAX3010x -> Install.
 *
 * ---------------------------------------------------------------------------
 * WIRING — XIAO ESP32S3 (port usbmodem101), NOT the Uno
 *
 *   SDA -> D4  (GPIO5)
 *   SCL -> D5  (GPIO6)
 *   VIN -> 3V3     <-- NOT 5V. The pull-ups tie to VIN and 5V exceeds the
 *   GND -> GND         3.6V GPIO maximum.
 *
 *   On every XIAO variant D4 is SDA and D5 is SCL. Wire.begin() with no
 *   arguments would land on the same two pins; they are named explicitly below
 *   so the intent survives a core update.
 * ---------------------------------------------------------------------------
 */

#include <Wire.h>
#include "MAX30105.h"

MAX30105 sensor;

// --- I2C PINS (XIAO ESP32S3) ---
// D4 = GPIO5 = SDA, D5 = GPIO6 = SCL. Fixed by the XIAO pinout.

#define SDA_PIN 5      // D4
#define SCL_PIN 6      // D5

// #define SDA_PIN 3      // D2
// #define SCL_PIN 4      // D3

// --- ACQUISITION CONFIG ---
// 400 Hz sampled, averaged by 4 on-chip -> 100 Hz out. 100 Hz is plenty for a
// PPG (useful content is under ~15 Hz) and gives Heart_Waveform_1.m's 2000
// point window exactly 20 s of trace.
//
// LED_BRIGHTNESS is back to 60 after a measured 190k DC with a finger on at
// 0x7F. 18-bit full scale is 262143, so 0x7F sat close enough to the ceiling to
// clip the AC component — and the AC is the entire signal, about 1% of DC. Too
// bright and too dim both produce a flat trace, so the saturation note in
// loop() reports the DC level rather than leaving this to guesswork.
const byte LED_BRIGHTNESS = 60;    // 0-255. Watch for the SATURATED note.
const long SATURATION     = 240000;  // 18-bit full scale is 262143
const byte SAMPLE_AVERAGE = 4;
const byte LED_MODE       = 2;     // 2 = Red + IR (both driven, see setup)
const int  SAMPLE_RATE    = 400;
const int  PULSE_WIDTH    = 411;

// ADC_RANGE was 4096 — the MOST sensitive setting (2048 nA full scale), which
// a resting finger at this LED current pins against the rail. A pinned reading
// is a CONSTANT reading, and a DC blocker fed a constant outputs exactly zero:
// a dead-flat trace from a sensor that is working too hard. Moving the finger
// dips it off the rail, which is why motion registered and stillness did not.
// 16384 buys two more octaves of headroom and is the usual choice for finger
// PPG. Raise to 32768 if the SATURATED note still fires.
const int  ADC_RANGE      = 16384;

// --- FILTER CONFIG ---
// Single-pole DC blocker: w[n] = x[n] + a*w[n-1];  y[n] = w[n] - w[n-1].
// Strips the huge DC pedestal (finger IR sits around 50k-200k counts) and
// leaves the ~1% pulsatile AC component.
//
// DC_ALPHA was 0.95. The corner of this filter is roughly (1-a)*fs/2*pi, so at
// 100 Hz that put it at ~0.8 Hz = 48 bpm — directly on top of a resting pulse.
// A 55 bpm heartbeat was being high-passed away as though it were drift, and
// what survived got truncated by the (long) cast on the way out. 0.99 moves the
// corner to ~0.16 Hz (~10 bpm): still far below any real pulse, so the pedestal
// is removed just as completely, but the beat itself now passes untouched.
const float DC_ALPHA  = 0.99f;

// Light EMA after the DC blocker. Removes sensor grain without visibly
// rounding the dicrotic notch, which matters if the derivative is being used
// to find feature points.
const float LPF_ALPHA = 0.35f;

// Derivative scaling. A plain sample-to-sample difference at 100 Hz is only a
// few hundred counts.
// ponytail: a plain gain knob, not a real dv/dt in units per second. The host
// now auto-scales its axes, so leaving this at 1 keeps the saved CSV in honest
// units; raise it only if you want the raw numbers pre-scaled.
const int DERIV_GAIN = 1;

// --- FINGER PRESENCE ---
// Below this the sensor is looking at air and the "waveform" is just noise
// amplified by the DC blocker. Ambient alone reads a few thousand on a working
// part, so this sits comfortably above resting and far below a loaded finger.
//
// Two thresholds, not one. With a single 5000 both ways, a marginally placed
// finger chatters across it: every dip re-primes the filter and restarts the
// settle blanking, so the output is mostly zeros with bursts of transient —
// which looks exactly like the fault being chased. The gap makes leaving
// deliberate.
const long FINGER_ON_THRESHOLD  = 5000;
const long FINGER_OFF_THRESHOLD = 3000;
bool fingerOn = false;

// --- CHANNEL SELECTION ---
// The SparkFun library is written for the MAX30105 (RED=LED1, IR=LED2). On
// many MAX30102 breakouts the two are swapped, so getFIFOIR() returns the dark
// channel, it never clears FINGER_THRESHOLD, and the host receives an endless
// stream of "0,0" — a flat line at exactly zero, with a healthy 100 Hz data
// rate, which is the most confusing failure this sketch has.
//
// Rather than make you read a verdict in MAX30102_RawCheck and edit a #define
// here, both LEDs are driven and the sketch locks onto whichever channel
// actually loads when a finger arrives. Works on either part, no edit needed.
const uint8_t CH_NONE = 0, CH_RED = 1, CH_IR = 2;
uint8_t chan = CH_NONE;

// Diagnostics throttle. Samples stream every loop; this only paces the "# "
// commentary so it cannot drown the console at 100 Hz.
const unsigned long NOTE_MS = 1000;
unsigned long lastNote = 0;

// --- ARRIVAL TRANSIENT ---
// A finger does not arrive in one sample: the reading ramps from ~2500 (air) to
// ~190000 over several samples, and the DC blocker faithfully reports that ramp
// as a ~2e5 step. That step is 200x larger than the PPG that follows it, so it
// dominates any auto-scaled axis and squashes the real waveform flat. Priming
// alone does not fix it — priming only zeroes the FIRST sample. Hold the output
// at zero for a second while the blocker converges, then start streaming.
// 2 s: the blocker's time constant is now 1/(1-DC_ALPHA) = 100 samples, so 100
// was only one constant of settling and let the tail of the step through.
const int SETTLE_SAMPLES = 200;   // 2 s at 100 Hz
int settle = 0;

// Per-second signal census. The old note printed one instantaneous `ac` value,
// which reads near zero at any moment a healthy pulse happens to cross its
// baseline — it could not distinguish a good signal from no signal at all.
// Min/max over the whole interval answers the question the note exists to ask.
uint32_t dcMin = 0xFFFFFFFF, dcMax = 0;
float    acMin = 1e9f,       acMax = -1e9f;

void resetCensus() {
  dcMin = 0xFFFFFFFF; dcMax = 0;
  acMin = 1e9f;       acMax = -1e9f;
}

float dcW      = 0.0f;   // DC blocker state
float lpfPrev  = 0.0f;   // EMA state
float filtPrev = 0.0f;   // previous filtered sample, for the derivative
bool  primed   = false;  // filter state valid?

void resetFilters() {
  dcW = 0.0f;
  lpfPrev = 0.0f;
  filtPrev = 0.0f;
  primed = false;
}

void setup() {
  Serial.begin(115200);
  delay(3000);   // ESP32S3 native USB CDC: without this the banner is lost

  // Bring up I2C on the XIAO's D4/D5 before handing the bus to the library.
  Wire.begin(SDA_PIN, SCL_PIN);

  // I2C_SPEED_FAST (400 kHz) — the default 100 kHz cannot keep up with the
  // FIFO at 400 Hz and samples get dropped.
  if (!sensor.begin(Wire, I2C_SPEED_FAST)) {
    while (true) {
      Serial.println("# MAX30102 NOT FOUND - check SDA=D4(GPIO5), SCL=D5(GPIO6), VIN=3V3");
      delay(1000);
    }
  }

  sensor.setup(LED_BRIGHTNESS, SAMPLE_AVERAGE, LED_MODE,
               SAMPLE_RATE, PULSE_WIDTH, ADC_RANGE);

  // BOTH LEDs stay lit. An earlier version did setPulseAmplitudeRed(0) to save
  // a few mA, on the reasoning that only the IR channel is read. That is safe
  // on a MAX30105, where getFIFOIR() reads the slot driven by LED2_PA.
  //
  // It is NOT safe on a MAX30102: on those modules the two channels are widely
  // reported swapped relative to what this MAX30105 library assumes, so the
  // slot getFIFOIR() reads is driven by LED1_PA — the register that
  // "optimisation" set to zero. Driving both costs a few mA, works on either
  // part, and is what makes the channel auto-selection below possible.
  sensor.setPulseAmplitudeRed(LED_BRIGHTNESS);
  sensor.setPulseAmplitudeIR(LED_BRIGHTNESS);

  resetFilters();
  Serial.println("# MAX30102 init OK - streaming filtered,derivative at 115200");
  Serial.println("# Rest a fingertip LIGHTLY on the window. Pressing hard occludes the");
  Serial.println("# capillaries and flattens the signal - a real cause of 'no reading'.");
}

void loop() {
  // check() pulls whatever the chip has buffered; available() then reports how
  // many samples are ready. Draining the FIFO in a loop stops the host seeing
  // bursts followed by gaps if this sketch is ever slowed down.
  sensor.check();

  while (sensor.available()) {
    uint32_t red = sensor.getFIFORed();
    uint32_t ir  = sensor.getFIFOIR();
    sensor.nextSample();

    // Decide the channel once per finger placement, never per sample: swapping
    // mid-trace would push a step through the DC blocker and read as a beat.
    long best = (long)((red > ir) ? red : ir);
    if (!fingerOn && best > FINGER_ON_THRESHOLD) {
      fingerOn = true;
      chan = (red > ir) ? CH_RED : CH_IR;
      resetFilters();
      Serial.print("# channel locked -> ");
      Serial.println(chan == CH_RED
        ? "getFIFORed()  *** this board has the channels SWAPPED ***"
        : "getFIFOIR()   (standard mapping)");
    }

    long ppg = (chan == CH_RED) ? (long)red : (long)ir;

    if (fingerOn && ppg < FINGER_OFF_THRESHOLD) {
      fingerOn = false;
      chan = CH_NONE;
    }

    if (!fingerOn) {
      // No finger. Reset the filters so re-applying one does not fire a huge
      // step transient through the DC blocker, release the channel lock so the
      // next placement re-tests it, and emit a flat line rather than amplified
      // noise. The throttled note says WHY the line is flat — without it this
      // is indistinguishable from a dead sensor at the host end.
      resetFilters();
      resetCensus();
      Serial.println("0,0");

      if (millis() - lastNote >= NOTE_MS) {
        lastNote = millis();
        Serial.print("# no finger  red="); Serial.print(red);
        Serial.print("  ir=");             Serial.print(ir);
        if (red < 2000 && ir < 2000) {
          // Ambient alone reads a few thousand on a working part, so this is
          // not "no finger" — it is no light reaching the photodiode. Run
          // MAX30102_RawCheck.ino for the register readback.
          Serial.println("   *** BOTH channels dark - LEDs not lighting ***");
        } else {
          Serial.println("   (resting level, put a finger on)");
        }
      }
      continue;
    }

    // Prime on the first valid sample so the DC blocker starts settled
    // instead of ramping from zero over the first second.
    if (!primed) {
      dcW = (float)ppg / (1.0f - DC_ALPHA);
      lpfPrev = 0.0f;
      filtPrev = 0.0f;
      primed = true;
      settle = SETTLE_SAMPLES;
    }

    // DC removal
    float wNew = (float)ppg + DC_ALPHA * dcW;
    float ac   = wNew - dcW;
    dcW = wNew;

    // Smoothing
    float filt = LPF_ALPHA * ac + (1.0f - LPF_ALPHA) * lpfPrev;
    lpfPrev = filt;

    // First derivative (plain sample-to-sample difference, scaled for display)
    float deriv = (filt - filtPrev) * (float)DERIV_GAIN;
    filtPrev = filt;

    // Filter state has advanced; the arrival step is simply not published.
    if (settle > 0) {
      settle--;
      Serial.println("0,0");
      continue;
    }

    // Census AFTER the settle gate, or the arrival transient lands in acMax and
    // the first ac_pp of every placement reads in the hundreds of thousands —
    // the same outlier problem the host's axis scaling had.
    if ((uint32_t)ppg < dcMin) dcMin = (uint32_t)ppg;
    if ((uint32_t)ppg > dcMax) dcMax = (uint32_t)ppg;
    if (filt < acMin) acMin = filt;
    if (filt > acMax) acMax = filt;

    // Exactly two integers and a newline. Nothing else.
    Serial.print((long)filt);
    Serial.print(',');
    Serial.println((long)deriv);

    // Periodic proof of life carrying the DC level, which the sample stream
    // deliberately throws away. A pulsatile AC of only a few counts on a
    // 100k pedestal means poor optical contact, not a broken filter.
    if (millis() - lastNote >= NOTE_MS) {
      lastNote = millis();
      long acPP = (long)(acMax - acMin);
      Serial.print("# finger  dc=");  Serial.print(dcMin);
      Serial.print("..");             Serial.print(dcMax);
      Serial.print("  ac_pp=");       Serial.print(acPP);

      if (dcMax > SATURATION) {
        // Pinned at the ADC ceiling the reading stops changing, so the DC
        // blocker outputs a constant zero: a flat trace from a sensor that is
        // working too hard. Raise ADC_RANGE or lower LED_BRIGHTNESS.
        Serial.println("   *** SATURATED - raise ADC_RANGE ***");
      } else if (dcMin == dcMax) {
        // Not saturated but not moving either: the FIFO is handing back the
        // same sample, which is an I2C fault, not an optical one.
        Serial.println("   *** DC FROZEN - FIFO not advancing (I2C) ***");
      } else if (acPP < 50) {
        // DC is alive and in range but carries no pulse. That is contact, not
        // configuration: too light and the light path leaks, too hard and the
        // capillaries are occluded. Both read as a flat line.
        Serial.println("   weak pulse - adjust finger pressure");
      } else {
        Serial.println("   pulse present");
      }
      resetCensus();
    }
  }
}
