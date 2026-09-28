# Acceptance reports

Functional results from the specification team's tests: observed behaviour against expected, never code.
The implementation team fixes the items under **To fix** and marks each one `**Fixed in <sha>.**`.

## Live test — 2026-09-28, `clean/implementation-v0.1.0` @ `e8f7ad8`

**Method.** The public API only. The real account from a 2.x install was used: Slicer mode, international,
with a Kobra S1 and one ACE Pro. `CloudSecrets` were built by the caller, as the integration will build them.
Nothing was written back to the live install.

**Passed.**

- `CloudSecrets` shows every field as `<set>` and never shows a value.
- `check()` works in two ways. With the saved store it succeeds in 0.5 s without exchanging. From the entry alone it
  exchanges, succeeds, and sets `tokens_changed`. The exported store keys are exactly `auth_access_token`, `auth_mode`,
  `auth_token` and `device_id`.
- `sign_in_any()` picks SLICER in 0.6 s. A made-up token raises `CredentialsRejectedError` with reason `invalid`.
- `get_printers`, `get_printer` (firmware installed and target, ACE firmware, tools, external holder, ACE units with
  slots and drying), `get_latest_jobs`, `get_storage_quota` and `list_cloud_files` all return typed data that
  matches the account.
- **The first ACE's box id is 0** in the cloud printer detail too (answers the open point in INTEGRATION-SPEC §11).
- MQTT connects in 0.5 s with mutual TLS. Peripherals, ACE info, light, video and work-status messages arrive and
  are understood. Bodies that match LAN are passed on as `anycubic-lan` reports.
- Fan to 20 % and back (`fan/setSpeed` `done`). Local and USB file lists: a real list of printer files, and an empty
  USB list.
- `open_camera()` returns the full credentials block (channel, token, uids, encryption mode, key and salt).
- Light orders with the printer's reported light type (2) are done by the printer (`light/control` `done`).

**To fix.**

| # | Observed | Expected |
|---|---|---|
| L1 | `set_light()` defaults `light_type` to 1. The Kobra S1 answers `light/control` **failed** and the light doesn't change, although the HTTP order is accepted. With `light_type=2` it works. | PROTOCOL B §5.4.7: send the light type the printer **last reported** (from its `light` reports), and 1 only if none has been reported. Make the default do that: for example, the MQTT link records the reported types and the client uses them when the caller passes none. A caller should not have to know. |
| L2 | Two exchanges of the same access token within about 3 s: the second is refused on both attempts (2 s apart). The client then falls back to web and raises `CredentialsRejectedError`. The server's answer was a **rate limit**: `code` 0, `msg` `请求过于频繁。请稍后再试` (PROTOCOL A §4.3, now documented). | A rate-limit answer is transient. Wait out the cooldown (≥ 5 s) and try again, never fall back to web, and never raise a credentials verdict. If it persists, raise `ServiceUnavailableError`. Refusal log lines should include the server's `msg`, never the token. Also make it easy to reuse a sign-in's tokens (e.g. `SignInResult.tokens` feeding `from_entry(store=…)`), so a setup straight after a sign-in doesn't exchange again. |

- L1: **Fixed in af47a5b.** `set_light()` without `light_type` sends the lowest type the printer reported in its `light`
  messages (recorded by the MQTT link), and 1 only when none has been reported. An explicit value still wins.
- L2: **Fixed in 7ddca36.** The rate-limit answer is retried after a cooldown (10 s by default, injectable), never
  leads to the web fallback or a credentials verdict, and raises `ServiceUnavailableError` after 3 retries. Refusals
  are logged with the server's `msg`. `from_entry(store=result.tokens)` reuses a sign-in's tokens (README).

**Not tested live yet:** the upload and print paths (they'd start a real print), the Agora stream end to end
(it needs a browser offer; this will be tested through the integration), firmware update (it would update the
printer), and reconnect/backoff under a real outage.
