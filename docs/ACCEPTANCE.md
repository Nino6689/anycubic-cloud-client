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

## Round 2 re-test — 2026-09-28, `clean/implementation-v0.1.0` @ `e5df26f`

Re-tested live against the same account.

- **L1 confirmed fixed.** `set_light()` with no `light_type` sent the type the printer had reported, and the printer answered
  `light/control` **done** for both off and on.
- **L2 confirmed fixed.** `sign_in_any()` followed at once by a fresh client from the entry alone, with no pause in between, signs in.
  Previously this ended in `CredentialsRejectedError`.
- The similarity check is unchanged (see `CLEAN-ROOM.md`), and the secret scan found no hits in 38 files.

**Result: v0.1.0 accepted.**

## Integration test — 2026-09-28, `hass-anycubic-next` PR #4 (`clean/cloud` @ `a60314f`) with v0.1.0

Through the integration, on the live account: sign-in, the config flow, polling, MQTT, commands and the
panel all worked. One library item was found.

| # | Observed | Expected |
|---|---|---|
| L3 | The cloud camera never streams. Opening it in Home Assistant fails with *"The camera channel is encrypted: pass the Agora SDK public key (sdk_public_key_pem)"*. The integration doesn't pass the key, and nothing tells it to. | Ship Agora's public key (PROTOCOL D §1.7, 'Agora's key', answer to Q6) as the default. `sdk_public_key_pem` becomes an optional override. Add a test that joins an encrypted channel without passing a key, checking that the join message carries `aes_secret` wrapped under that key: decrypt it in the test with a throwaway key only when the override is used, and otherwise check its length and base64. |

- L3: **Fixed in 92225c7.** `agora.AGORA_SDK_PUBLIC_KEY` holds Agora's key (base64 SPKI text from PROTOCOL D §1.7) and is
  the default; `sdk_public_key_pem` is only an optional override. Tests join an encrypted channel with no key passed (the
  join carries `aes_secret` as base64 of exactly 128 bytes) and with a throwaway override key (the secret decrypts to the
  channel key's raw UTF-8 bytes).
