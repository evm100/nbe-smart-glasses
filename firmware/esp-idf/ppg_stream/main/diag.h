// Why the chip last reset ("poweron", "brownout", "panic", "task_wdt"...), for the BOOT event and
// the MQTT status: tells a supply dip on battery from a firmware crash.
#pragma once

const char *diag_reset_reason(void);
