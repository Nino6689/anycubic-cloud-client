# anycubic-cloud-client

An async Python client for the **Anycubic cloud**: account sign-in, the cloud
HTTP API, the cloud MQTT feed, files, printing and the cloud camera's
signalling. It is written for the `anycubic_cloud` Home Assistant integration
(version 3.0), but it has no Home Assistant code.

**Status:** specification. The protocol description is in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md) and the API the integration needs is in
[`docs/INTEGRATION-SPEC.md`](docs/INTEGRATION-SPEC.md). The code is written
under clean-room rules ([`docs/CLEAN-ROOM.md`](docs/CLEAN-ROOM.md)).

## No Anycubic credentials inside

Anycubic has no public API. Its cloud expects the credentials of its own apps:
client and app identifiers, an app secret, and a TLS client certificate and key
for the MQTT broker. This library **does not contain any of them**. The caller
passes them in when it creates a client, and releases are checked for key
material before publishing. The library works as far as those credentials let
it. It is not affiliated with or endorsed by Anycubic.

If Anycubic publishes an official developer API, this library will move to it.

## Licence

MIT, see [`LICENSE`](LICENSE).
