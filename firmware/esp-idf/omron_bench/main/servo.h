// SG90 button pusher, like auto-cuff/servo_cuff.py: drive toward the press angle for PRESS_MS, then
// stop sending pulses so the button's spring pushes the limp arm back.
#pragma once
#include "esp_err.h"

esp_err_t servo_init(int gpio);
void servo_press(int pulse_us, int ms);
