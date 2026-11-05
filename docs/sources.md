# Source manuals (not in the repo)

Download from the manufacturer and put the file into `data/raw/`. The checksum identifies the exact version the evaluation questions were written against. Manuals are copyrighted: they stay local.

| # | Document | Type | Fault information | sha256 |
|---|----------|------|-------------------|--------|
| 1 | Sungrow SG305HX / SG320HX-20 / SG350HX-20 User Manual (149 pp, 2024) | string inverter | section 8.1, pp. 127-135: table "Fault Code / Fault Name / Corrective Measures" without grid lines, read as plain text by design-ingest v0.3.0 | `6883bccb432cc52f9c31e56602806cb78a4fe4952992a064d0c3089c0483b94b` |
| 2 | Huawei "Inverter Alarm Reference", Issue 14, 2026-06-15 (122 pp) | string inverter alarms | one section per alarm ID (e.g. 2002, 2034): tables "Alarm ID / Name / Severity" and "Cause ID / Suggestion", already read as tables | `f0f92b7633576e430ba17f6866018fc54a886e83239b416ce67c59feefea858b` |
| 3 | SMA Sunny Central 500CP-JP / 630CP-JP / 800CP-JP / 1000CP-JP User Manual, version 2.3 (SCCP-JP-B1-BA-en-23, 150 pp) | utility (central) inverter | section 7 Troubleshooting, from p. 64: event numbers (e.g. 3504, 3515) with W/W flags and corrective measures, read as plain text | `744a69a188103eb57e56d095107aea992b44d71766629f97ea6b2de6c83c28dc` |

Where to get them: each manufacturer's support / download center (search the title). Exact URLs are not recorded here because they change.
