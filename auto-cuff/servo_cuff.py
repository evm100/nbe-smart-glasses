#!/usr/bin/env python3
"""
SG90 button pusher for Raspberry Pi (Zero 2 W, Raspberry Pi OS Trixie).

Each press drives the servo toward 180 degrees for 150 ms, then stops sending
pulses. The button's spring pushes the limp arm back, releasing the button.

Wiring:
    Brown  -> GND      (pin 6)
    Red    -> 5V       (pin 2 or 4)
    Orange -> GPIO18   (pin 12)

Command line:
    python3 servo_button.py         # press once

In your program:
    from servo_button import ButtonPusher

    with ButtonPusher() as pusher:
        pusher.press()
"""

import time

from gpiozero import AngularServo

SERVO_PIN = 18        # BCM numbering
PRESS_ANGLE = 180     # degrees
PRESS_MS = 150        # milliseconds the servo is driven per press


class ButtonPusher:
    def __init__(self, pin=SERVO_PIN, press_angle=PRESS_ANGLE, press_ms=PRESS_MS):
        self.press_angle = press_angle
        self.press_ms = press_ms
        self._servo = AngularServo(
            pin,
            min_angle=0,
            max_angle=180,
            min_pulse_width=0.0005,
            max_pulse_width=0.0024,
            initial_angle=None,  # start limp
        )

    def press(self):
        """Boop the button once."""
        self._servo.angle = self.press_angle
        time.sleep(self.press_ms / 1000)
        self._servo.detach()

    def close(self):
        self._servo.detach()
        self._servo.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


if __name__ == "__main__":
    with ButtonPusher() as pusher:
        pusher.press()
