---
name: system_diagnostics
description: Diagnostic tool for investigating system telemetry, hardware metrics, and disk health.
triggers:
  - system status
  - system diagnostics
  - hardware stats
  - check disk
  - system health
tools:
  - get_system_status
  - list_processes
---

## System Diagnostics Instructions
When reporting system diagnostics:
- State exact metrics clearly with units (MB, GB, %, Celsius).
- Alert the user if resource load or storage space is near capacity.
