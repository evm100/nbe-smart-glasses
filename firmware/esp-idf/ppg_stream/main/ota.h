// Over-the-air update (MQTT link only), see ota.c.
#pragma once

// Called once from link_init(): notes whether the previous update was rolled back.
void ota_init(void);
// After the first broker connection: keep the running image (cancels the bootloader's rollback).
void ota_mark_valid(void);
// For the status message: "" | "downloading 40%" | "failed: ..." | "rebooting" | "updated" | "rolled back"
const char *ota_state(void);
// First 8 hex digits of the running app's ELF SHA-256 (tells two builds of one version apart).
const char *ota_build_id(void);
