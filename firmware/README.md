# Living Margins ESP32 firmware

Target hardware: Waveshare ESP32-S3-Touch-LCD-4.3 (800x480, ESP32-S3 N16R8).

The first milestone is a hardware baseline: initialize the RGB display and GT911 touch controller with the vendor-supported configuration, render a restrained Living Margins status screen, and confirm touch input before networking is added.

Local build and upload:

```powershell
..\.venv\Scripts\python.exe -m platformio run
..\.venv\Scripts\python.exe -m platformio run --target upload
```

The original 16MB factory image is backed up locally under `runtime/firmware-backups/`, which is intentionally excluded from Git.


The current firmware polls the live reading state, renders Chinese comments, persists agree/disagree state across refreshes, and authenticates feedback requests with a revocable per-device token stored only in `device_secrets.h`.
