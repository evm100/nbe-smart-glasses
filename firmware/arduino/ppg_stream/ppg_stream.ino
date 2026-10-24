// PPG serial protocol v1 – reference implementation for ESP32 + MAX30102
// See ../PROTOCOL.md. Library: "SparkFun MAX3010x Pulse and Proximity Sensor Library".
//
// REFERENCE CODE: written to the protocol spec but NOT yet compiled or tested on
// hardware. Treat it as a starting point for the real firmware.
//
// Note: the SparkFun library buffers only 4 samples by default (STORAGE_SIZE in
// MAX30105.h). At 400 Hz either call check() very often (as below) or raise
// STORAGE_SIZE to 32.

#include <Wire.h>
#include "MAX30105.h"
#include "esp_timer.h"

// ---------------- configuration ----------------
static const char* DEVICE_ID = "FNG01";   // unique per board, write it on the label
static const char* FW_VERSION = "0.1.0";
static const char* SITE = "finger";       // finger | glasses | ...
static const int   SDA_PIN = 8;
static const int   SCL_PIN = 9;
static const int   BUTTON_PIN = -1;       // marker button to GND, -1 = none
static const long  BAUD = 921600;

// MAX30102 settings (these are reported in the H line)
static const byte  LED_BRIGHTNESS = 0x1F; // 0x1F ~ 6.4 mA
static const float LED_MA = 6.4;
static const byte  SAMPLE_AVG = 1;        // 1,2,4,8,16,32
static const byte  LED_MODE = 2;          // 2 = red + IR
static const int   SAMPLE_RATE = 400;     // sensor rate; output fs = SAMPLE_RATE / SAMPLE_AVG
static const int   PULSE_WIDTH = 411;     // 69,118,215,411 us
static const int   ADC_RANGE = 16384;

static const int   FS_OUT = SAMPLE_RATE / SAMPLE_AVG;
static const int64_t PERIOD_US = 1000000LL / FS_OUT;

MAX30105 sensor;
static bool streaming = false;
static uint32_t seq = 0;
static int64_t lastStatus = 0;
static uint32_t maxLoopUs = 0;
static int lastButton = HIGH;
static char line[192];
static char cmd[32];
static uint8_t cmdLen = 0;

// ---------------- protocol helpers ----------------
static void sendBody(const char* body) {
  uint8_t cs = 0;
  for (const char* p = body; *p; ++p) cs ^= (uint8_t)*p;
  Serial.printf("$%s*%02X\r\n", body, cs);
}

static void sendHeader() {
  snprintf(line, sizeof line,
           "H,proto=1,dev=%s,fw=%s,sensor=MAX30102,fs=%d,ch=ir|red,adc=18,"
           "led_ma=%.1f|%.1f,pw_us=%d,avg=%d,site=%s",
           DEVICE_ID, FW_VERSION, FS_OUT, LED_MA, LED_MA, PULSE_WIDTH, SAMPLE_AVG, SITE);
  sendBody(line);
}

static void sendEvent(const char* code, const char* text = "") {
  snprintf(line, sizeof line, "E,%lld,%s,%s", (long long)esp_timer_get_time(), code, text);
  sendBody(line);
}

static void handleCommand(const char* c) {
  if (strcmp(c, "INFO") == 0) {
    sendHeader();
  } else if (strcmp(c, "START") == 0) {
    seq = 0;
    sensor.clearFIFO();
    sendHeader();
    sendEvent("START");
    streaming = true;
  } else if (strcmp(c, "STOP") == 0) {
    streaming = false;
    sendEvent("STOP");
  }
}

static void pollCommands() {
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (cmdLen) { cmd[cmdLen] = 0; handleCommand(cmd); cmdLen = 0; }
    } else if (cmdLen < sizeof(cmd) - 1) {
      cmd[cmdLen++] = ch;
    }
  }
}

// ---------------- setup / loop ----------------
void setup() {
  Serial.begin(BAUD);
  Wire.begin(SDA_PIN, SCL_PIN);
  if (!sensor.begin(Wire, I2C_SPEED_FAST)) {
    while (true) { sendEvent("SENSOR_ERR", "MAX30102 not found"); delay(1000); }
  }
  sensor.setup(LED_BRIGHTNESS, SAMPLE_AVG, LED_MODE, SAMPLE_RATE, PULSE_WIDTH, ADC_RANGE);
  if (BUTTON_PIN >= 0) pinMode(BUTTON_PIN, INPUT_PULLUP);
  sendEvent("BOOT");
  sendHeader();
}

void loop() {
  int64_t t0 = esp_timer_get_time();
  pollCommands();

  if (streaming) {
    // check() reads every sample waiting in the sensor FIFO into the library buffer
    uint16_t n = sensor.check();
    if (n > 0) {
      int64_t now = esp_timer_get_time();
      uint16_t k = 0;
      while (sensor.available()) {
        uint32_t red = sensor.getFIFORed();
        uint32_t ir = sensor.getFIFOIR();
        sensor.nextSample();
        // back-calculate each sample's time from its position in the batch
        int64_t t = now - (int64_t)(n - 1 - k) * PERIOD_US;
        snprintf(line, sizeof line, "D,%lu,%lld,%lu,%lu",
                 (unsigned long)seq++, (long long)t, (unsigned long)ir, (unsigned long)red);
        sendBody(line);
        k++;
      }
      if (n >= 32) sendEvent("FIFO_OVF", "fifo full - samples may be lost");
    }

    if (BUTTON_PIN >= 0) {
      int b = digitalRead(BUTTON_PIN);
      if (b == LOW && lastButton == HIGH) sendEvent("BTN");
      lastButton = b;
    }

    if (t0 - lastStatus > 1000000) {
      lastStatus = t0;
      snprintf(line, sizeof line, "S,%lld,loop_us_max=%lu", (long long)t0, (unsigned long)maxLoopUs);
      sendBody(line);
      maxLoopUs = 0;
    }
  }

  uint32_t dt = (uint32_t)(esp_timer_get_time() - t0);
  if (dt > maxLoopUs) maxLoopUs = dt;
}
