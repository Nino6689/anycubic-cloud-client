# What the `anycubic_cloud` integration needs from this library

Requirements from the specification team. The wire protocol is in
[`PROTOCOL.md`](PROTOCOL.md), and section numbers like "A §2.6" refer to its
parts. Entity meanings are in `Nino6689/hass-anycubic-next` `docs/BEHAVIOUR.md`,
and stored-data and config-entry formats are in its `docs/COMPAT.md`. The
library has **no Home Assistant code**. The integration persists state, runs
the config flow, and builds entities.

Names below are suggestions. Behaviour is the requirement.

## 1. Shape of the library

- Async throughout, on `aiohttp` (the caller passes its `ClientSession`).
  MQTT on `paho-mqtt`, with a thread-safe hand-off to the event loop, as
  `anycubic-lan` does it.
- Python ≥ 3.13, fully typed (`py.typed`), `mypy --strict` clean, `ruff`
  clean, ≥ 95 % test coverage, no network in tests.
- It depends on **`anycubic-lan`** and reuses its report parser wherever
  PROTOCOL Part C says a cloud MQTT body is the same as the LAN body. Only the
  differences are parsed here.
- Typed, frozen dataclasses for everything the integration reads. Each keeps the
  raw `dict` it came from, so new fields reach diagnostics without a release.
  One bad field never discards a whole answer; the rule is the same as
  `anycubic-lan`'s.
- No `print`. Logging goes through `logging.getLogger(__name__)`. Tokens,
  signatures and anything in `CloudSecrets` are **never** logged or put in a
  `repr`.

## 2. Anycubic's credentials: `CloudSecrets`

The library contains none of Anycubic's credentials (`CLEAN-ROOM.md`). The caller builds
one immutable object and passes it to every client:

| Field | Type | Role (PROTOCOL A §0.1) |
|---|---|---|
| `app_id` | `str` | signs every HTTP request |
| `app_secret` | `str` | signs every HTTP request |
| `client_id_web` | `str` | persisted only (A §2.4) |
| `client_id_app` | `str` | persisted only |
| `mqtt_ca_pem` | `bytes` | pins the MQTT broker, and its public key encrypts the slicer-mode MQTT password (A §2.12) |
| `mqtt_client_cert_pem` | `bytes` | MQTT mutual TLS |
| `mqtt_client_key_pem` | `bytes` | MQTT mutual TLS |

- Its `repr` shows only which fields are set, never their values.
- The object is validated on creation: non-empty strings, and PEM text that parses.
  An invalid object raises a dedicated error before any network traffic.
- Tests use obviously fake values and a CA, certificate and key generated at test time.

## 3. Regions, modes and tokens

- `Region`: `international` and `china`, resolved from any stored value by
  A §1.1 (unknown values resolve to international). `AuthMode`: `WEB = 1`,
  `ANDROID = 2`, `SLICER = 3` (A §2.3).
- **Token state** in and out, matching the keys of the 2.x token store
  (A §2.10, COMPAT §6): `auth_token`, `auth_access_token`, `device_id`,
  `auth_mode`. The library imports a dict with those keys and exports one. It
  never writes `app_id`, `app_secret`, `app_version` or `app_client_id` into
  the export. Those keys are read and ignored.
- The client reports when its tokens have changed (a new exchange, or the web
  fallback), so the integration saves the store (A §2.10).
- **Token helpers** for the config flow (BEHAVIOUR §5.8, A §2.1, A §2.11):
  - extract a token from pasted text or a Slicer Next config file;
  - decode the claims without verifying them (`exp`, `tokenType`, `iss`);
  - verify an RS256 signature against the auth domain's JWKS, using a browser-like
    user agent and a 15 s timeout;
  - trim an over-long signature to the key's length.

## 4. Signing in and checking

- `check()` implements A §2.6.5 steps 4–5:
  - exchange an access token when needed, with 2 attempts 2 s apart;
  - fall back to web once (A §2.8);
  - drop a displaced user token and exchange again, once.
  - It returns the account: user id (int), email, mobile, and the account identifier (A §2.6.1).
- A §2.6.5 step 6, the retry without the store, is the **integration's** job:
  the library just makes building a client from the entry alone easy.
- `sign_in_any(token, device_id, region, secrets)` for the config flow tries
  modes in the order of A §2.9. It returns the winning mode, the account and the
  token state. When every mode fails it says whether the token has the wrong
  `tokenType`.
- **Errors**, which must stay distinct, because re-authentication may only follow a
  credentials verdict (A §4.4):

  | Error | When |
  |---|---|
  | credentials rejected, with a reason: invalid / expired / wrong token type | A §4.3 rows 1–3, after every retry |
  | service unavailable | transport error, timeout, non-JSON body, `msg` = `request error` |
  | unexpected response | an answer without the expected data shape |
  | printer removed | `code` 1007 |
  | order refused, with the server's `msg` | an order answered with null `data` |
  | file not found | `msg` = `No file found` |
  | secrets invalid | §2 |

- Slow calls: log one warning when a call takes over 20 s, **at most once
  per 10 minutes** (A §3.8; 2.x warned on every slow call). An optional flag
  logs each call's URL and duration at debug level (the entry option
  `debug_api_calls`).

## 5. Account, printers and jobs (HTTP)

Methods for every endpoint 2.x calls, in PROTOCOL Part B, returning typed
results:

- **Printers:** the list (E4) and each printer's detail (E6). The detail
  includes firmware (installed and target), peripherals and capabilities,
  ACE boxes, and the external holder. Follow B §10's quirks: a single ACE comes
  back as an object rather than a list, and an absent holder is all nulls.
- **Jobs:** the job list (E12) with the "latest job" rule; job detail (E13),
  the only source of speed-mode names and temperature limits; FDM sliced-file
  detail (E16); job image URLs (X2).
- Fetch the job list **once per poll for all printers**, not once per printer as
  2.x does (B §8.2).
- Printer rename (E11), and the firmware update start for the printer (E24)
  and for each ACE (E25).

## 6. Orders (HTTP `sendOrder`)

A generic `send_order(printer, order_id, data=None, project_id=None)`, plus one
method per order the integration uses (B §5, D §0.2). The body shape
matters: `order_id` must be a string or an integer exactly as B §5.1 says,
and `project_id` must be present where it says. The server acknowledges
wrong shapes as "Operation successful" and the printer silently ignores them.
Methods needed:

- pause, resume and cancel the current job;
- light on and off with brightness;
- fans, target temperatures and speed mode;
- axis moves, home and motors off;
- ACE: feed, retract, drying start and stop per box, slot info, auto refill;
- request the local or USB file list; delete a local or USB file;
- AI detection on and off (D §5.1);
- open the camera (§8).

The reply carries a `msgid`, and the result of an order arrives on MQTT. The method returns the
`msgid`, so the integration can match the reply.

## 7. Files and printing

- Cloud file list (E17, paged), delete (E18) and storage quota (E19).
- **Upload** (B §4.4, D §2.5): quota → lock → PUT → register → unlock. The
  unlock must run **whether or not** the upload succeeded. In 2.x a failed
  upload left the lock behind.
- **Start a print** (order 1, D §2.1–§2.4) from a cloud file, a file on the
  printer or USB stick, or a fresh upload. Print settings:
  - the ACE slot mapping (`ams_info.ams_box_mapping`);
  - `ai_detect` and `camera_timelapse`, which default to 0 as in 2.x;
  - the file's own paint colour, not the slot's (in 2.x the slot's was sent, D §2.4).
- Gcode metadata from an uploaded file's header (D §2.6).
- `No file found` is an error, never a success (in 2.x three such replies counted as
  success, with message id "None").

## 8. Cloud camera

- `open_camera(printer)` sends order 1001 exactly as D §1.3 says: the string
  `"1001"` and a top-level `shengwang_rtc_support: true`. It returns the
  credentials block as a typed object (D §1.4).
  - When the block is missing, force a fresh exchange and try once more (A §5.2).
  - Credentials are single-use and never cached.
  - A second failure raises "no camera, or another Anycubic session holds the account".
- Whether a printer has a camera comes from the peripherals report (D §1.2).
- The **Agora WebRTC signalling client** also lives in this library. It turns the
  credentials into a WebRTC answer for Home Assistant's camera: D §1.7 lists
  every input and the Anycubic-specific steps beyond a generic client. It
  may be adapted from the MIT project `Jezza34000/homeassistant_petkit`,
  keeping its licence notice and crediting it in `README.md`.

## 9. Cloud MQTT

- One MQTT client per account (PROTOCOL Part C covers the connection, identity,
  topics and envelope). The integration decides **when** it runs, following
  its `mqtt_connect_mode` option (C, BEHAVIOUR §5.1), so the library offers:
  - connect;
  - disconnect;
  - is-connected;
  - subscribe and unsubscribe a printer;
  - a connection listener, for lost and restored links;
  - a parsed-message listener;
  - a raw-message listener, for diagnostics and the file-list replies.
- The login is worked out again on every reconnect, because tokens may have changed (A §2.12).
- Reconnects back off, and a wrong port or refused login must end in a clear error.
  2.x looped silently.
- A `code` of 200 in the envelope is success (C). Null payloads follow the
  rules in C, and never clear known state.

## 10. Firmware progress

- Parse the `ota` messages into a progress figure by the rule in BEHAVIOUR §2.17.
- **Updating ends when the printer reports the new version.** A cloud refresh
  that already shows the new installed version must also clear it. In 2.x the
  flag could stay stuck on (D §4.4).

## 11. Open points carried into implementation

Take the conservative choice, mark it in code with a reference to the
question, and add the question to `QUESTIONS.md`. Open points:

- **The first ACE's box id.** A cloud capture shows `id: 1`, while LAN reports and the firmware list use
  0 (B §11, D Open points).
  - Slot mapping must work with either.
  - The specification team will confirm on hardware.
- **The success `code` on HTTP answers.** Judge success by `data`, as 2.x does (A §4.2).
- **China's MQTT port and API path.** Assumed the same as international (A §1.2).
