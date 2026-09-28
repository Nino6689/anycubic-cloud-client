# anycubic-cloud-client

An async Python client for the **Anycubic cloud**: account sign-in, the cloud
HTTP API, the cloud MQTT feed, files, printing and the cloud camera's
signalling. It is written for the `anycubic_cloud` Home Assistant integration
(version 3.0), but it has no Home Assistant code.

The protocol is described in [`docs/PROTOCOL.md`](docs/PROTOCOL.md) and the API
the integration needs in [`docs/INTEGRATION-SPEC.md`](docs/INTEGRATION-SPEC.md).
The code is written under clean-room rules ([`docs/CLEAN-ROOM.md`](docs/CLEAN-ROOM.md)).
Open questions are in [`docs/QUESTIONS.md`](docs/QUESTIONS.md).

## No Anycubic credentials inside

Anycubic has no public API. Its cloud expects the credentials of its own apps:
client and app identifiers, an app secret, and a TLS client certificate and key
for the MQTT broker. This library **does not contain any of them**. The caller
passes them in a `CloudSecrets` object when it creates a client, and releases
are checked for key material before publishing. The library works as far as
those credentials let it.

This project is **not affiliated with or endorsed by Anycubic**. If Anycubic
publishes an official developer API, this library will move to it.

## Install

```sh
pip install anycubic-cloud-client
```

Python 3.13 or later. Dependencies: `aiohttp`, `paho-mqtt`, `cryptography`,
`bcrypt` and [`anycubic-lan`](https://github.com/Nino6689/anycubic-lan), whose
report parser is reused for the MQTT bodies the cloud shares with LAN Mode.

## Usage

```python
import asyncio

import aiohttp

from anycubic_cloud_client import (
    AnycubicCloudClient,
    AnycubicCloudError,
    AuthMode,
    CloudMqttClient,
    CloudSecrets,
    CredentialsRejectedError,
    Region,
)


async def main(secrets: CloudSecrets, pasted_token: str, stored: dict) -> None:
    async with aiohttp.ClientSession() as session:
        cloud = AnycubicCloudClient.from_entry(
            session,
            secrets,
            token=pasted_token,  # the Slicer Next access token
            auth_mode=AuthMode.SLICER,
            region=Region.INTERNATIONAL,
            store=stored,  # the saved token store, if any
        )
        try:
            account = await cloud.check()  # exchange + userInfo, with retries
        except CredentialsRejectedError as err:
            print("ask the user for a new token:", err.reason)
            return
        if cloud.tokens_changed:
            stored = cloud.export_token_store()  # save it, then:
            cloud.mark_tokens_saved()

        printers = await cloud.get_printers()
        latest = await cloud.get_latest_jobs([p.id for p in printers if p.id])
        for printer in printers:
            detail = await cloud.get_printer(printer.id)
            print(detail.name, detail.nozzle_temp, latest[printer.id])

        link = CloudMqttClient(cloud)
        link.add_message_listener(lambda message: print(message.kind, message.update))
        for printer in printers:
            link.subscribe_printer(printer.key, printer.machine_type)
        await link.connect()
        try:
            await link.wait_until_connected()  # never send before the link exists
            await cloud.query_peripherals(printers[0].id)  # the answer comes over MQTT
            await asyncio.sleep(30)
        except AnycubicCloudError as err:
            print("cloud error:", err)
        finally:
            await link.disconnect()


# The caller builds CloudSecrets from wherever it keeps Anycubic's app
# credentials; this library never ships them.
secrets = CloudSecrets(
    app_id="...",
    app_secret="...",
    client_id_web="...",
    client_id_app="...",
    mqtt_ca_pem=b"-----BEGIN CERTIFICATE-----...",
    mqtt_client_cert_pem=b"-----BEGIN CERTIFICATE-----...",
    mqtt_client_key_pem=b"-----BEGIN RSA PRIVATE KEY-----...",
)
```

`CloudSecrets` is validated when it is built and its `repr` never shows a
value. Tokens, signatures and secrets are never logged.

### Setting up right after a sign-in

The cloud refuses a second exchange of the same access token within a few
seconds (a rate limit, PROTOCOL A §4.3). The client waits it out (10 s by
default, `rate_limit_delay=` to change it) and raises
`ServiceUnavailableError`, never a credentials error, if it persists. To
avoid it altogether, reuse the tokens of the sign-in instead of exchanging
again:

```python
result = await sign_in_any(session, secrets, pasted_token)
stored = result.tokens.to_store()  # save this as the token store
cloud = AnycubicCloudClient.from_entry(
    session,
    secrets,
    token=pasted_token,
    auth_mode=result.auth_mode,
    store=result.tokens,  # a TokenState or a saved store dict
)
await cloud.check()  # userInfo only: no second exchange
```

### Lights

`set_light(printer_id, on)` sends the light type the printer reported over
the cloud MQTT link (the lowest one), and 1 only when none has been reported
yet (PROTOCOL B §5.4.7). The link records the types as `light` messages
arrive; the printer id is tied to its key by `get_printers()` or
`get_printer()`. To restore types remembered across a restart, call
`cloud.note_light_types(printer_key, types)`. An explicit `light_type=` wins.

## What is supported

- **Sign-in** in the three modes (web, Android, slicer) and both regions
  (international, China): the token exchange with its retry and its
  rate-limit cooldown, the web fallback, the displaced-token retry, `sign_in_any()` for a config flow, the
  2.x token-store format in and out, and token helpers (extraction from
  pasted text or a Slicer Next config file, unverified claims, the RS256
  pre-check against the JWKS with signature trimming).
- **Errors that stay distinct**: credentials rejected (invalid, expired, wrong
  token type), service unavailable, unexpected response, printer removed,
  order refused, file not found, secrets invalid. Only the first may lead to
  re-authentication.
- **HTTP**: printers and their detail (firmware, ACE units, external holder,
  capabilities), the job list (fetched once for all printers) and job
  detail, sliced-file detail, rename, printer and ACE firmware updates.
- **Orders** in their exact body shapes: pause, resume, cancel, light, fans,
  temperatures, speed mode and print settings, axis moves and homing,
  motors off, ACE feed/retract/drying/slot/auto-refill, file lists and
  deletes on the printer and USB stick, AI detection, peripherals and light
  queries, and a generic `send_order()`.
- **Files and printing**: cloud file list, delete and quota; upload (the
  storage lock is always released, also after a failure); print a cloud file,
  a printer or USB file, or a fresh upload, with the ACE slot mapping and
  G-code header parsing.
- **Cloud MQTT**: identity per mode, the pinned-CA TLS context, subscription
  and routing, parsing of every message kind (reusing `anycubic-lan` where the
  body is the LAN body), reconnect with back-off, printers subscribed as soon
  as they are added, a clear error when the broker refuses the login, and
  firmware update progress.
- **Cloud camera**: the camera-open order with its retry after a fresh login,
  and an Agora WebRTC **signalling** client that turns the credentials and a
  browser's SDP offer into an SDP answer (no media passes through it).
  Encrypted channels work out of the box: the library ships Agora's public
  key from its Web SDK (`agora-rtc-sdk-ng` 4.24.0, PROTOCOL D §1.7) as the
  default, so a caller passes nothing. `sdk_public_key_pem` stays as an
  optional override ([Q6](docs/QUESTIONS.md)).

## Credits

The Agora signalling (`agora.py`, `agora_sdp.py`) is adapted from the Agora
client of [homeassistant_petkit](https://github.com/Jezza34000/homeassistant_petkit)
by @Jezza34000, used under its MIT licence; its copyright notice is kept in
those files.

## Licence

MIT, see [`LICENSE`](LICENSE).
