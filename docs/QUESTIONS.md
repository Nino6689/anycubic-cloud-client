# Questions

Questions from the implementation team, with the specification team's
answers. The implementation team adds a question here, and marks its interim
choice in the code, whenever a fact it needs is missing from `docs/`.

Asked by the implementation team for v0.1.0 (2026-09-28). Each interim choice
is marked in the code with its number (`Qn in docs/QUESTIONS.md`).

| # | Question | Interim choice in v0.1.0 |
|---|---|---|
| Q1 | After the web fallback (PROTOCOL A §2.8, Open point 12), should the requests carry the WEB header values (`Xx-Device-Type: web`, `Xx-Version: 1.0.0`), or keep 2.x's mixed set? | Keep 2.x's mixed set: the per-mode values of the mode the client was built with, plus the browser `User-Agent` and `Origin` because the mode is now WEB. It is the only set known to work. |
| Q2 | Which box id does the first ACE report on each transport (B §11 O2, D Open point 3)? The capture shows `multi_color_box.id` 1 for a single ACE. | The slot mapping offsets by the unit's **position** in the list (0, 1, ...), so it works whether the first ACE reports 0 or 1. ACE orders (drying, feed, slot, refill) send the id the caller gives. |
| Q3 | Under which key does a colour-list entry (`paint_infos` from `infoFdm`, `paint_info` from the G-code header) carry the file's own colour, and in what form? INTEGRATION-SPEC §7 asks for the file's paint colour in `paint_color`. | Look for `paint_color`, then `color`, as `[r, g, b(, a)]` or `"#RRGGBB"`. When neither is present, send the slot's colour, as 2.x did. |
| Q4 | What is the success value of the HTTP envelope `code` on each endpoint (A §4.2, B §11 O1)? | Not read: success is judged by `data`, as in 2.x (INTEGRATION-SPEC §11). |
| Q5 | Are China's MQTT port, API path and image base the same as international's (A §1.2)? | Assumed the same: port 8883, `p/p/workbench/api`, the same image base. Only China's hostname check is waived. |
| Q6 | The encrypted camera channel needs the RSA public key embedded in `agora-rtc-sdk-ng` 4.24.0 (D §1.7). The clean-room rules list only petkit as an allowed outside source, so this team did not fetch the npm package. May the key be put in the library, and if so, who extracts it? | The caller passes the key (`sdk_public_key_pem`) to `AgoraCameraSession`; without it, joining an encrypted channel raises a clear `AgoraError`. The library contains no key material. |
| Q7 | How long may a firmware update run before "in progress" is ended by a timeout (D §4.4 asks for one)? | One hour without any `ota` message (`FirmwareProgress.timeout`, adjustable). |
| Q8 | After a link that worked, how many consecutive refused logins (CONNACK not authorised) should end the reconnect loop (C §6.7, Open point 12)? | 3, each with freshly worked-out credentials; then the client stops and reports `MqttAuthError`. Network errors keep the 5 s → 120 s back-off indefinitely. A refusal on the first connect raises at once. |
| Q9 | Is `code` 1007 used by endpoints other than printer info and status? | It is checked on every call and raises `PrinterRemovedError` (never a credentials verdict). |
| Q10 | Should the user-topic slicing reports (U1, U2) reach the integration? | Subscribed and logged at debug, as in 2.x; passed to the raw-message listener only. |
| Q11 | Which `Content-Type` does the pre-signed storage PUT require (B §11 O4)? | None set by the library; aiohttp's default for raw bytes (`application/octet-stream`) applies. |
| Q12 | How does an ACE printer choose slots for a file already on the printer, and should `ams_info` be sent (D Open point 5)? | No `ams_info` for printer-held files, as 2.x. |
| Q13 | Names and placement of the other print-time options (levelling, flow calibration, vibration compensation, dry first) in order 1 (B §11 O6, D Open point 1)? | Only `task_settings.ai_detect` and `camera_timelapse` are sent; both default to 0. |
| Q14 | Should the firmware-update query's `target_version` be the installed version (as 2.x sends, and which works) or the offered one (D Open point 10)? | The installed version, as 2.x. |
| Q15 | A printer-topic payload without `type`: 2.x logs an ERROR. Is it worth more than a debug line? | Dropped with a debug line (BEHAVIOUR B36: unknown input is logged quietly). |

**Answer to Q6 (specification team, 2026-09-28):** yes, ship it. The key is Agora's public key from its published Web SDK. That makes it protocol data, not a credential. Its value and provenance are now in PROTOCOL D §1.7 ('Agora's key'). Make it the library's default, keep `sdk_public_key_pem` as an optional override, and never require the caller to supply it. See ACCEPTANCE L3.
