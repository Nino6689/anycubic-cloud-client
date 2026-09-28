# Anycubic cloud protocol

The specification team's description of the Anycubic cloud as the
`anycubic_cloud` 2.x integration uses it. It was collected on 2026-09-28 from that
integration and its API library at the versions named in each part, their tests and
captured payloads, and field notes. It is written as facts and required behaviour only;
there is no code (`CLEAN-ROOM.md`).

| Part | Covers | Cited elsewhere as |
|---|---|---|
| A | Regions, the three sign-in modes, tokens, request signing, the response envelope, session behaviour | A §n, AUTH |
| B | Every HTTP endpoint: parameters, response shapes, orders, uploads, firmware, when 2.x calls what, server quirks | B §n, HTTP |
| C | Cloud MQTT: connection, identity, topics, envelope, every message, lifecycle | C §n, CMQTT |
| D | The cloud camera (Agora), printing, file management, firmware updates, other cloud-only features | D §n |

Conventions:

- **Anycubic's credentials** appear only as the `CloudSecrets` field that
  carries them, e.g. `secrets.app_secret`. Their values are not in this
  repository (INTEGRATION-SPEC §2).
- **Personal data** in examples is replaced by `<placeholders>`.
- **"2.x"** is the behaviour of the GPL integration being replaced. Where a part
  says 2.x has a bug, the requirement is **not** to reproduce it, unless the part
  says compatibility needs it.
- `BEHAVIOUR.md`, `COMPAT.md` and `DECISIONS.md` are in
  `Nino6689/hass-anycubic-next/docs/`. `LAN §n` is `Nino6689/anycubic-lan`
  `docs/PROTOCOL.md`.
- Each part ends with its **open points**. The implementation team makes the
  conservative choice and asks in `QUESTIONS.md`.

## Part A. Cloud: regions, authentication and HTTP request construction

Specification-team facts for the clean 3.0 cloud client. Collected 2026-09-28
from `anycubic-cloud-api` 0.4.32 (the library 2.9.4 pins), the `anycubic_cloud`
2.9.4 integration, their tests, the maintainer's field notes, and Anycubic's own
Slicer Next UI script (for the success code only). Facts and required behaviour
only; no code.

Related documents, not repeated here:

- `hass-anycubic-next/docs/COMPAT.md` §1 (config entry keys), §6 (the
  `anycubic_cloud.<entry id>` token store shape).
- `hass-anycubic-next/docs/BEHAVIOUR.md` §5.1 (poll cadence), §5.6 (token store,
  expiry repair, re-auth), §5.7 (setup outcomes), §5.8 (config flow, local token
  pre-checks), §6 B6/B7/B28–B31.

---

### 0. Conventions

#### 0.1 Anycubic's credentials, by name only

The cloud only answers requests signed with Anycubic's own application
credentials. They are Anycubic's data. This document never gives their values.
Each is named by its role, as a field of the `CloudSecrets` object the caller
passes in (INTEGRATION-SPEC §2).

| Role | `CloudSecrets` field | Format (not the value) | Sent on the wire? |
|---|---|---|---|
| App id (all modes) | `secrets.app_id` | 32 alphanumeric characters | No. Used only inside the signature. |
| App secret (all modes) | `secrets.app_secret` | 32 alphanumeric characters | No. Used only inside the signature. |
| OAuth client id, web | `secrets.client_id_web` | 20 alphanumeric characters | No, never in 2.x. Only persisted (§2.10). |
| OAuth client id, apps (slicer and Android) | `secrets.client_id_app` | 20 alphanumeric characters | No, never in 2.x. Only persisted. |
| MQTT TLS CA, client certificate, client key | `secrets.mqtt_ca_pem`, `secrets.mqtt_client_cert_pem`, `secrets.mqtt_client_key_pem` | PEM | MQTT only, not HTTP. The CA's public key is also used to encrypt the slicer MQTT password (§2.12). |

These are **not** secret and are given as values below, because every request
sends them in clear or they are ordinary addresses: the app version strings
(web `1.0.0`, Android `1.4.8`,
slicer `V3.0.0`, sent as `Xx-Version`), the browser
user-agent string, domain names, and API paths (the API path
`p/p/workbench/api` is a plain path).

#### 0.2 Words

| Word | Meaning |
|---|---|
| **Access token** | The Casdoor-issued JWT the Slicer Next app holds (`tokenType` claim `access-token`). The user pastes it. It is **not** accepted by the workbench API directly (international region). |
| **User token** | The token the workbench API accepts in `XX-Token`. The website keeps one in its browser storage. The slicer obtains one by exchanging its access token (§2.6.3). |
| **Workbench API** | The printer-cloud HTTP API under `https://<base domain>/p/p/workbench/api`. |
| **Auth domain** | Anycubic's account server (a Casdoor instance). It issues access tokens and publishes their signing keys. |
| **Mode** | One of the three auth modes (§2.3). Stored as an int. |

---

### 1. Regions

#### 1.1 Values and resolution

- Two regions: **`international`** and **`china`**. Stored in config entry data
  under `region` (COMPAT §1).
- Anycubic runs them as two separate deployments. Accounts, printers and
  servers are not shared: a China account does not exist internationally and
  the reverse also holds. The region names the service that owns the account.
  It is not a language or preference setting.
- Resolution of a stored value never fails: trim it, lower-case it, and match it
  against the two names. Anything else (absent, empty, wrong type, unknown text)
  resolves to **`international`**. Entries from before the field existed, and
  LAN-only entries, have no region and resolve to international.
- The region is fixed for the lifetime of a client object. Changing it means
  rebuilding the client, because the signed headers, the Origin, the MQTT
  client and any in-flight session are all bound to one deployment.
- The user always picks the region explicitly. It is **never auto-detected**.
  Two reasons: the token's issuer claim does not tell the regions apart (the
  China token in hass-anycubic #13 carried the international issuer), and
  probing the other deployment would send the user's bearer token to a second
  operator.

#### 1.2 Per-region endpoints

| Item | `international` | `china` | China status |
|---|---|---|---|
| Base domain (workbench API host) | `cloud-universe.anycubic.com` | `cloud-platform.anycubicloud.com` | Reported by one user (#13); not verified by the maintainer |
| Auth domain (Casdoor, and `Origin` in web mode) | `uc.makeronline.com` | `uc.makeronline.cn` | Reported (#13); not verified |
| Workbench API path (the API path (`p/p/workbench/api`)) | `p/p/workbench/api` | same | Assumed, not reported |
| API root | `https://cloud-universe.anycubic.com/p/p/workbench/api` | `https://cloud-platform.anycubicloud.com/p/p/workbench/api` | Derived |
| MQTT broker host | `mqtt-universe.anycubic.com` | `mqtt.anycubicloud.com` | Reported (#13) |
| MQTT port | `8883` (MQTT over TLS) | `8883` | Assumed. A wrong port gives a silent endless reconnect loop, not an error. |
| MQTT broker hostname check | **on** | **off** | Field-verified: the China broker's certificate does not name `mqtt.anycubicloud.com`. Chain verification against the pinned Anycubic CA and the client certificate stay on in both regions. Only the hostname comparison is waived, and only for China. |
| Project image URL base | `https://workbentch.s3.us-east-2.amazonaws.com/` (the misspelling is Anycubic's, and required) | same | Assumed. If wrong, the only effect is a missing thumbnail. |
| Web token source page | `https://cloud-universe.anycubic.com/file` | unknown | — |

#### 1.3 URL construction

- Base URL = `https://` + base domain + `/` (with a trailing slash).
- API root = base URL + `p/p/workbench/api`. The API path has no leading or
  trailing slash.
- Request URL = API root + endpoint path. Every endpoint path starts with `/`.
  So there is exactly one `/` at each join. A doubled or missing slash was
  singled out as a risk, because it shows up in the field as "server
  maintenance" (see §4.4).
- Example: `https://cloud-universe.anycubic.com/p/p/workbench/api/user/profile/userInfo`.
- Project image URL: when a project's own image URL is not an absolute `http…`
  URL, it is the region's image base joined with the project's
  `slice_param.image_id`.

#### 1.4 Other behaviour that depends on the region

| Behaviour | international | china |
|---|---|---|
| A pasted slicer token is exchanged for a user token (§2.6.3) | yes | **no**. The pasted token is used directly as the user token, because China's slicer holds a user-type token and MQTT login needs one. |
| Config-flow `wrong_token_type` check (BEHAVIOUR §5.8) | yes | skipped |
| `Origin` header in web mode | `https://uc.makeronline.com` | `https://uc.makeronline.cn` |
| JWKS fetched for the local signature pre-check | `https://uc.makeronline.com/.well-known/jwks` | **the same international URL**. In 2.x the pre-check is not region-aware. It only runs for tokens whose `iss` is `https://uc.makeronline.com`, and an empty key set is accepted. |
| `Xx-Is-Cn` header | by mode, not by region (§3.2) | same. 2.x does not change it for China (see Open points). |

---

### 2. Authentication

#### 2.1 Two systems and their tokens

- The **auth domain** (`uc.makeronline.com`) is a Casdoor instance. It issues
  the slicer's **access tokens**, which are RS256 JWTs. It publishes its signing
  keys at `https://<auth domain>/.well-known/jwks`. That endpoint answers 403 to
  non-browser user agents, so it must be fetched with a browser-like one.
- The **workbench API** (`cloud-universe.anycubic.com`) accepts **user tokens**.
  Per the maintainer's notes these are HS512 JWTs with claims such as `user_id`,
  `mode` and `isCN`, no `iss`, and no published key, so they cannot be verified
  locally.
- `POST /v3/public/loginWithAccessToken` (§2.6.3) turns an access token into a
  user token.

| Token | Where the user gets it | Typical shape | Lifetime |
|---|---|---|---|
| Slicer access token (international) | Anycubic Slicer Next config file, JSON key path `anycubic_cloud` → `access_token` (macOS `~/Library/Application Support/AnycubicSlicerNext/AnycubicSlicerNext.conf`, Windows `%APPDATA%\AnycubicSlicerNext\AnycubicSlicerNext.conf`). Newer slicer builds AES-encrypt that section, and the token then has to be recovered from the running process's memory. | About 1200 characters. JWT starting `eyJ`. Header `alg` RS256. Claims include `tokenType` = `access-token`, `iss` = `https://uc.makeronline.com`, `aud`, `scope`, `exp`, `iat`, plus identity claims. The `kid` in the header may differ from the published key's `kid`. That is normal: the key still verifies. | `exp` about 90 days after issue. No refresh is available. |
| Web user token | The website's browser storage, key `XX-Token`, on the signed-in web page (§1.2) | About 238 characters. Also a JWT starting `eyJ`, per the maintainer's notes. Parts of 2.x still allow for an opaque, non-JWT web token, and extraction accepts one (BEHAVIOUR §5.8). | Not recorded (see Open points) |
| Android user token and device id | Captured from the Android app's traffic (proxy) | not recorded | not recorded |
| China slicer token | China Slicer Next | Described in 2.x as an HS512 user token. One report says it carried the international `iss`. | not recorded |

Facts about token rejection (fingerprinted on the international service,
issue #8):

- The server gives the same message, **"User does not exist"**, for *any*
  invalid token: a flipped signature, a truncated signature, a tampered
  payload, a string that is not a JWT at all, a token of the wrong
  `tokenType`, or a web token sent to the exchange. The message never means
  anything about the account.
- A token recovered from memory can have correct claims and still be dead,
  because extra bytes are stuck to the end of its signature. An RS256
  signature is exactly as long as the key's modulus. That is
  ceil(bits/8) bytes, which is ceil(bytes × 8 / 6) base64url characters. So an
  over-long signature can be cut to that length and checked again
  (BEHAVIOUR §5.8).

#### 2.2 No password login

The integration never asks for an email or password. Automatic login is not
possible:

- the Casdoor applications have the password grant disabled for both client
  ids;
- there is no device-code grant;
- redirect URIs are locked to the app scheme (`anycubic-i18n://…`);
- the web login has a captcha and 2FA.

A pasted token is the only route. The library defines an OAuth-token endpoint
(`GET /v3/public/getoauthToken`) and a redirect URI constant
(the app redirect URI = `anycubic-i18n://cloud.anycubic.com:8088`). **Neither is
used by 2.x.**

#### 2.3 The three modes

| | WEB | ANDROID | SLICER |
|---|---|---|---|
| Stored int (`user_auth_mode`, store `auth_mode`) | **1** | **2** | **3** |
| User supplies | the web user token | a user token **and** a device id | the slicer access token (international) or the slicer token (China) |
| `Xx-Device-Type` | `web` | `android` | `pcf` |
| `Xx-Is-Cn` | `1` | `0` | `1` |
| `Xx-Version` (constant) | `1.0.0` | `1.4.8` | `V3.0.0` |
| App id / app secret | `secrets.app_id` / `secrets.app_secret` | same | same |
| OAuth client id (persisted only) | `secrets.client_id_web` | `secrets.client_id_app` | `secrets.client_id_app` |
| Nonce style (§3.3) | UUID text | 22-character packed | UUID text |
| `XX-Device-Id` header | no | **yes** | no |
| Browser `User-Agent` and `Origin` | **yes** | no | no |
| Token exchange before use | no | no | yes (international); no (China) |
| Cloud MQTT login possible | **no** | yes | yes |
| MQTT app id (in MQTT username) | `app` (unused) | `app` | `pcf` |

If no mode is given (an entry without `user_auth_mode`), the mode is **WEB**.

#### 2.4 Identifiers other than the token

- **App id, app secret, version**: fixed per mode, from the constants above.
  2.x never discovers them at run time (§2.5).
- **Client id**: chosen per mode and written to the token store as
  `app_client_id`. No request uses it.
- **Android device id**: the user supplies it with the token. If an Android
  client has none, 2.x makes one up the first time the header is built:
  **33 lowercase hex characters**. These are the first 33 characters of two
  time-based (version 1) UUIDs, each written as 32 hex characters without
  hyphens and joined end to end. Once made, it is kept for the client's
  lifetime and persisted in the store's `device_id`. In practice 2.x only
  selects Android when a device id was entered, so the made-up id is a
  fallback that should never be needed.

#### 2.5 WEB-mode run-time discovery: defined but never used

The 2.x library (and every vendored copy since the fork began) defines
patterns for scraping the web app's credentials out of its JavaScript. **No
code calls them.** WEB mode uses the fixed constants in §2.3. The patterns are
listed so that nobody thinks discovery is required:

| Pattern role | What it matches (in words) |
|---|---|
| App script | In an HTML page, a `src="…"` attribute whose value is a path `/js/app.<segment without dots>.js`. The path is captured. |
| Client id | A single-quoted string of exactly 20 letters or digits, not starting with `getEl`. A commented-out older variant looked for `,clientId:"<value>",`. |
| App id, plain form | The key `appid` (optionally quoted), a colon, a quoted value, then a comma |
| App id, minified form | A single-quoted string of exactly 32 letters or digits that does not directly follow a colon |
| Version | The key `version` (optionally quoted), a colon, a quoted value, then a comma |
| App secret, plain form | The key `appSecret` (optionally quoted), a colon, a quoted value, then a comma |
| App secret, minified form | The same pattern as the minified app id |

The page that would be fetched, and how the id and secret would be told apart
in minified form, cannot be determined from the sources (see Open points).

#### 2.6 Login sequences

All requests are built as in §3. "userInfo" means
`GET /user/profile/userInfo` with the `XX-Token` header.

##### 2.6.1 userInfo: the check common to all modes

1. Send userInfo with the current user token.
2. Read the envelope's `data`:
   - `msg` equal to `request error` → **transient** failure (server
     maintenance or rate limiting; §4.3). This check runs before the data
     check.
   - `data` null → credentials **rejected**.
   - `data` present but `id` null or missing → credentials **rejected**. A
     refused token can return a data object with no id.
   - Otherwise accepted. Record:
     - `id`, as an integer. It becomes the config entry unique id, as a string
       (COMPAT §1). If it cannot be read as an integer it is left unset.
     - `user_email`. The server sends `""` rather than leaving it out; treat
       `""` as absent. China accounts have none.
     - `mobile`. Also treat `""` as absent.
3. **Account identifier** = email if present, else mobile, else the decimal
   user id. It is the new entry's title and part of the MQTT identity (§2.12).
4. Other `data` fields seen, all personal and to be redacted from diagnostics:
   `birthday`, `password`, `message_key`, `last_login_ip`, `casdoor_user_id`,
   `casdoor_user`, `user_nickname`, `ip_country`, `ip_province`, `ip_city`,
   `create_time`, `create_day_time`, `last_login_time`.

##### 2.6.2 WEB and ANDROID

1. The user token is the pasted token as-is.
2. userInfo (§2.6.1). For ANDROID the request also carries `XX-Device-Id`.
3. Accepted → logged in. There is no other step.

##### 2.6.3 SLICER, international (token exchange)

1. The pasted token is kept as the **access token**. The user token starts
   empty.
2. **Exchange**:
   - Request: `POST /v3/public/loginWithAccessToken`, signed as in §3, **without**
     `XX-Token`.
   - JSON body: `device_type` = `pcf`, then `access_token` = the access token.
     `device_type` follows the client's mode (`android`, `pcf` or `web`), but
     only slicer clients reach this call.
   - Success: `data` is non-empty and `data.token` holds the **user token**.
   - Failure: `data` null, empty or missing. The envelope's `msg` explains why.
     Two messages are known: `User does not exist` (any invalid token, §2.1)
     and `Login information has expired. Please login again.` (the session has
     been revoked on the server, §5.1).
   - A failure is retried once: **2 attempts in total, 2 s apart**. Only an
     empty-`data` answer is retried. A transport or parse error propagates
     straight away.
3. If both attempts fail, run the **web fallback** (§2.8).
4. userInfo (§2.6.1) with the user token.

##### 2.6.4 SLICER, China

The pasted token is used directly as the user token, as in §2.6.2. There is no
exchange, and the access token stays empty. Headers are the slicer set.

##### 2.6.5 Order of work in a setup (2.x), protocol view

Integration-level outcomes are in BEHAVIOUR §5.6–§5.7.

1. Build a client for the entry's region.
2. Load the entry's token, mode and device id. For SLICER outside China, move
   the token into the access-token slot and leave the user token empty.
3. Overlay the token store (§2.10): only the keys `auth_token`,
   `auth_access_token` and `device_id`, and only those present in the store. A
   stored `null` does overwrite: a present key replaces the value even when
   null.
4. **Check** the credentials:
   - (a) If the client is SLICER, holds an access token and has no user token,
     exchange it (§2.6.3), with the web fallback if needed.
   - (b) Run userInfo.
5. If the check failed and the client is SLICER holding **both** tokens, drop
   the user token and repeat step 4. This forces a new exchange and replaces a
   stale or displaced user token.
6. If the check still failed **and** step 3 loaded a store, rebuild the
   credentials from the entry alone (steps 2 and 4–5, with no overlay). This is
   the required fix in §5.3.
7. Still failing → authentication failed (re-auth). Passing → save the store.

#### 2.7 Re-validation and "refresh"

- There is **no refresh grant** and no refresh token. The pasted token lasts
  about 90 days. The only warning is the expiry repair (BEHAVIOUR §5.6), which
  reads `exp` from the **pasted** token without verifying it.
- **Every cloud poll** (at most every 60 s, BEHAVIOUR §5.1) repeats steps 4–5 of
  §2.6.5, so a userInfo request is made on every poll. The one thing that looks
  like a "refresh" is step 5: when the stored user token stops working, the
  access token is exchanged again for a new one.
- When a poll changes the tokens (a new exchange, or the web fallback), the
  store is saved again. If the poll check fails, the entry asks for re-auth.

#### 2.8 Web fallback ("retry as web")

Web user tokens and slicer access tokens are both JWTs starting `eyJ`. Nothing
reliable in the token itself separates them, so a web token can be mistaken for
a slicer token. The server's rejection is used as the signal:

- **When**: the client is SLICER, holds an access token, has no user token, and
  the exchange failed both attempts.
- **What**: move the access token into the user-token slot, clear the access
  token, switch the mode to WEB, and mark the tokens as changed. Then continue
  to userInfo.
- It fires **at most once**. Without an access token, or with a user token
  already held, there is nothing to retry and the check fails.
- **Header quirk in 2.x**: only the mode changes. The per-mode values are left
  as they were set at construction. After the fallback, requests therefore
  carry `Xx-Device-Type: pcf`, `Xx-Is-Cn: 1` and `Xx-Version: V3.0.0`, **and**
  the browser `User-Agent` and `Origin`, because those follow the mode. 2.x
  relies on the international service accepting this mixed set. It is how the
  web-token fix for #7 works. It has not been checked separately.
- **Persistence quirk in 2.x**: the config flow saves the mode it *tried*
  (3, slicer) in the entry's `user_auth_mode`, while the store saves the
  fallback's mode (1). On later setups the store overlay gives the client a
  user token and no access token, so no exchange happens. Headers are then the
  pure slicer set **without** User-Agent and Origin, and the client believes
  MQTT login is possible.
- **3.0 must accept entries whose `user_auth_mode` is 3 but whose token is a
  web token**. They exist in the field.

#### 2.9 Picking the mode when it is not known (config flow)

Token extraction and the local pre-checks come first (BEHAVIOUR §5.8 steps 1–3).
Then:

1. **First guess**:
   - a device id was entered → ANDROID;
   - otherwise the token starts with `eyJ` → SLICER;
   - otherwise → WEB.
2. **Order tried**:
   - with a device id: **ANDROID only**;
   - otherwise: the first guess, then the other of SLICER and WEB. A JWT gives
     SLICER then WEB. An opaque token gives WEB then SLICER.
3. Each attempt uses a **new client** in the chosen region. The first success
   wins, and the mode tried is what gets saved.
4. What decides:
   - SLICER (international) fails when the exchange's `data` is empty (the
     message is usually `User does not exist`). The in-client web fallback
     (§2.8) runs before the flow moves on to its next mode.
   - Any mode fails when userInfo returns no user object or no `id`.
   - `msg` = `request error`, a transport error, or unparseable JSON is **not**
     a credentials verdict. It ends that attempt with an error (§4.4).
5. When every mode fails:
   - if the JWT has a `tokenType` claim that is not `access-token` (not
     checked for China), show `wrong_token_type`;
   - otherwise show `invalid_auth`.

#### 2.10 What is persisted

| Where | Key | Holds |
|---|---|---|
| Entry data (COMPAT §1) | `user_token` | The token the user pasted, after extraction and signature trimming. For SLICER this is the **access** token. |
| | `user_auth_mode` | The mode that succeeded in the flow (1/2/3). May be 3 for a web token (§2.8). |
| | `user_device_id` | Android device id, else null |
| | `region` | `international` or `china` |
| Token store `anycubic_cloud.<entry id>`, version 1 (COMPAT §6) | `auth_token` | The **user token** currently in use (for SLICER, the one from the exchange), or null |
| | `auth_access_token` | The access token (SLICER international), else null. Null after the web fallback. |
| | `device_id` | Android device id (entered or made up), else null |
| | `auth_mode` | The client's mode when saved, as an int. 1 after a web fallback. |
| | `app_id`, `app_secret`, `app_version`, `app_client_id` | The constants in force for the mode the client was **built** with. These are Anycubic credentials: never log them and never put them in diagnostics. |

Rules:

- 2.x **reads back only `auth_token`, `auth_access_token` and `device_id`**.
  The stored `auth_mode` and `app_*` values are written but ignored. The entry's
  mode and the constants always win.
- If the per-entry store is empty, the legacy store with the bare key
  `anycubic_cloud` (same shape) is read and copied to the per-entry key.
- When the store is written: after every successful setup, unconditionally;
  after a poll in which the tokens changed; and it is deleted by a new paste
  (§5.3).

#### 2.11 Local token pre-checks

Covered in BEHAVIOUR §5.8 steps 1–3: extraction, the expiry check, and the
RS256 signature check against the JWKS (15 s timeout, browser-like user agent),
including signature trimming. Protocol facts in §2.1 above.

#### 2.12 What authentication gives the MQTT login (for cross-reference)

The MQTT document is authoritative. These are the auth-side inputs and how 2.x
combines them. Here md5 means lowercase hex MD5 of UTF-8 text.

| Item | How it is formed |
|---|---|
| Allowed | SLICER or ANDROID mode, with a user token held. Never WEB. |
| Account identity string | Email if present, else mobile. If neither, MQTT cannot start. |
| MQTT client id | md5(identity string), or md5(identity string + `pcf`) for SLICER |
| MQTT password, SLICER | The user token encrypted with RSA PKCS#1 v1.5 under the public key of the pinned Anycubic CA certificate, then base64 |
| MQTT password, ANDROID | bcrypt (fresh salt) of md5(user token) |
| MQTT username | `user` \| MQTT app id (`pcf` or `app`) \| account identifier (§2.6.1) \| md5(client id + password + client id), joined with `\|` |

The user id feeds the MQTT topics (md5 of the decimal user id). On a reconnect
the login is worked out again, because the tokens may have changed since.

---

### 3. HTTP request construction

#### 3.1 Transport

| Aspect | 2.x behaviour |
|---|---|
| Scheme | HTTPS only |
| TLS | The HTTP client's standard TLS context: public CA bundle, full chain verification, hostname check on, no client certificate. The pinned Anycubic CA, client certificate and SECLEVEL relaxation apply **only to MQTT**. |
| Session | One HTTP session per client object, with its own cookie jar. The jar accepts cookies from IP-address hosts too. No known flow depends on cookies. |
| Timeouts | None set per request. The HTTP client defaults apply: in Home Assistant's aiohttp, 300 s total and 30 s to connect. The JWKS pre-check alone uses 15 s. |
| Redirects | The client's default: followed. Home Assistant's session refuses redirects to local addresses. |
| HTTP status | **Never inspected.** Every response body is parsed as JSON whatever its status. |
| Response parsing | The body must be JSON with a JSON content type. Anything else (HTML error page, timeout, DNS failure, connection reset) becomes the single error "Unexpected error parsing Anycubic response, server maintenance?" (§4.4). |

#### 3.2 Headers

Sent on every workbench API request, in the order 2.x builds them. Names are
spelled exactly as 2.x sends them. HTTP header names are case-insensitive, but
note the two spellings `Xx-` and `XX-`.

| Header | Value | When |
|---|---|---|
| `Xx-Device-Type` | `web` / `android` / `pcf` (§2.3) | always |
| `Xx-Is-Cn` | `1` for web and slicer, `0` for Android | always |
| `Xx-Nonce` | a fresh nonce for each request (§3.3) | always |
| `Xx-Signature` | §3.4 | always |
| `Xx-Timestamp` | Unix time in **milliseconds**, as a decimal integer string | always |
| `Xx-Version` | the mode's version string (§2.3) | always |
| `Content-Type` | `application/json` | always, **including GET** |
| `XX-Device-Id` | the Android device id | ANDROID mode only |
| `XX-Token` | the user token, raw, with **no** `Bearer` prefix | every request except the token exchange |
| `XX-LANGUAGE` | `US` | always |
| `User-Agent` | `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36` | WEB mode only (checked against the current mode, so it also applies after the web fallback) |
| `Origin` | `https://` + region auth domain | WEB mode only. The library can suppress it or override it per call; 2.x never does. |

In SLICER and ANDROID modes no `User-Agent` is set, so the HTTP client's
default is sent. Under Home Assistant that is
`HomeAssistant/<version> aiohttp/<version> Python/<major>.<minor>`, and the
service accepts it.

#### 3.3 Nonce

- **WEB and SLICER**: a new time-based (version 1) UUID in its standard text
  form. That is 36 characters: lowercase hex in groups 8-4-4-4-12, joined by
  hyphens.
- **ANDROID**: 22 characters, made from a new version-1 UUID:
  1. Take the UUID's 16 bytes and read them as two **signed** 64-bit big-endian
     integers, *hi* (bytes 0–7) and *lo* (bytes 8–15).
  2. Encode each into 11 characters and join them: encode(*hi*) followed by
     encode(*lo*).
  3. To encode *n*: start with 11 `0` characters. For positions 10, 9, 8, … 0,
     while *n* is neither 0 nor −1:
     - write alphabet[*n* AND 61] at that position;
     - replace *n* with *n* shifted right by 6 bits, **arithmetic** (sign
       kept).
     - Positions not reached stay `0`.
  4. The alphabet is the 62 characters `0`–`9`, then `a`–`z`, then `A`–`Z`.
     The mask is **61**, not modulo 62, so only 32 of the 62 characters can
     appear. That is how 2.x does it. Whether the server checks the nonce's
     format is unknown.

#### 3.4 Signature

signature = md5_hex( app_id ‖ timestamp ‖ app_version ‖ app_secret ‖ nonce ‖ app_id )

- ‖ is plain string concatenation with no separators, encoded as UTF-8.
- app_id is `secrets.app_id` and app_secret is `secrets.app_secret`, the same in every
  mode.
- app_version is the value sent in `Xx-Version`.
- timestamp is the exact string sent in `Xx-Timestamp`.
- nonce is the exact string sent in `Xx-Nonce`.
- The output is 32 **lowercase** hex characters.
- The app id appears **twice**, first and last.
- The method, path, query and body are **not** signed.

#### 3.5 Query, body and content type

| Method | Query string | Body |
|---|---|---|
| GET | the call's parameters, URL-encoded into the query string | none |
| POST | none on any current endpoint (the library could add one) | the parameters as JSON text. A POST with no parameters sends `{}`. 2.x writes JSON with a space after `,` and after `:`, keys in insertion order, non-ASCII escaped as `\uXXXX`. The body is not signed, so the formatting is not believed to matter. |
| PUT | — | Used only for the cloud-upload pre-signed storage URL, not the workbench API: raw file bytes, **no** Anycubic auth headers (WEB mode still adds User-Agent and Origin). An empty response body means success. Any response text is an error. |

#### 3.6 Worked example (placeholders)

```
GET https://cloud-universe.anycubic.com/p/p/workbench/api/user/profile/userInfo
Xx-Device-Type: pcf
Xx-Is-Cn: 1
Xx-Nonce: 5f0c6e2a-9c1d-11f1-8b7e-0242ac120002
Xx-Signature: <md5 hex of AID + "1759050000000" + "V3.0.0" + SEC + nonce + AID>
Xx-Timestamp: 1759050000000
Xx-Version: V3.0.0
Content-Type: application/json
XX-Token: <user token>
XX-LANGUAGE: US
User-Agent: <HTTP client default>
```

The token exchange, same headers without `XX-Token`:

```
POST https://cloud-universe.anycubic.com/p/p/workbench/api/v3/public/loginWithAccessToken
body: {"device_type": "pcf", "access_token": "<access token>"}
→ {"code": <n>, "msg": "<text>", "data": {"token": "<user token>", …}}
```

#### 3.7 Retries

| Where | Retry |
|---|---|
| Any single request | **none** in the library |
| Token exchange | 2 attempts, 2 s apart, only on an empty-`data` answer; then the web fallback once (§2.8). **Required in 3.0:** a rate-limit answer (§4.3) is retried after the cooldown (≥ 5 s) and never counts as a refusal. A client created right after a sign-in should reuse that sign-in's tokens rather than exchange again. |
| Displaced user token | once per check: drop the user token and exchange again (§2.6.5 step 5) |
| Stored tokens refused at setup | once: rebuild from the entry's token alone (§2.6.5 step 6) |
| Setup, on the "server maintenance / request error" class | 3 retries, 10 s apart (4 attempts), then a **terminal** setup error. BEHAVIOUR §5.7 describes this as "retried 3 times". |
| Poll failures | back-off after 3 consecutive failures (BEHAVIOUR §5.1) |
| Camera-open answered without credentials | once, after forcing a fresh exchange (§5.2) |

#### 3.8 Slow-call warning and debug logging

- Each request is timed from send until its body is parsed. If the elapsed time,
  cut down to whole seconds, is **more than 20 s**,
  log a warning of the form "Responses from server are taking over 20s (Took
  Ns)".
- Intended limit: **at most one such warning per 10 minutes**
  (600 s). In 2.x the time of the last warning
  is never recorded, so **every** slow response warns. 3.0 should apply the
  10-minute limit.
- When the entry option `debug_api_calls` is on, each call logs its URL (no
  query string, no headers) and its duration at debug level. Tokens and
  signatures are never logged.

#### 3.9 Endpoint index (method and parameter placement only)

All paths are relative to the API root (§1.3). Payload fields belong to the
other cloud documents. Keep the capitals exactly as shown (`/v2/Printer/status`,
`/work/printer/Info`).

| Name | Method | Path | Parameters | Token |
|---|---|---|---|---|
| token exchange | POST | `/v3/public/loginWithAccessToken` | body | **no** |
| OAuth token (unused) | GET | `/v3/public/getoauthToken` | — | — |
| user info | GET | `/user/profile/userInfo` | none | yes |
| user storage | POST | `/work/index/getUserStore` | body `{}` | yes |
| lock storage space | POST | `/v2/cloud_storage/lockStorageSpace` | body | yes |
| unlock storage space | POST | `/v2/cloud_storage/unlockStorageSpace` | body | yes |
| register uploaded file | POST | `/v2/profile/newUploadFile` | body | yes |
| delete cloud files | POST | `/work/index/delFiles` | body | yes |
| list cloud files | POST | `/work/index/files` | body | yes |
| printer status | GET | `/v2/Printer/status` | query | yes |
| printer info | GET | `/v2/printer/info` | query | yes |
| printer tool (unused) | GET | `/v2/printer/tool` | — | yes |
| printer functions (unused) | GET | `/v2/printer/functions` | — | yes |
| all printers | GET | `/v2/printer/all` | none | yes |
| printers status | GET | `/work/printer/printersStatus` | none | yes |
| get printers | GET | `/work/printer/getPrinters` | none | yes |
| print history | GET | `/v2/project/printHistory` | none | yes |
| project info | GET | `/v2/project/info` | query | yes |
| project monitor | GET | `/v2/project/monitor` | query | yes |
| get projects | GET | `/work/project/getProjects` | query | yes |
| gcode info (FDM) | GET | `/work/gcode/infoFdm` | query | yes |
| send order | POST | `/work/operation/sendOrder` | body | yes |
| rename printer | POST | `/work/printer/Info` | body | yes |
| printer firmware update | GET | `/work/printer/update_version` | query | yes |
| ACE firmware update | POST | `/v2/printer/update_multi_color_box_version` | body | yes |

---

### 4. Response envelope

#### 4.1 Fields

Every workbench API answer is a JSON object with:

| Field | Type | Meaning |
|---|---|---|
| `code` | int | result code (§4.2) |
| `msg` | str | human-readable result or reason, in English |
| `data` | object, list, string or null | the payload. Null, empty or missing when the call failed. |

Some endpoints put more top-level keys next to these. For example, the
printer-rename and firmware-update answers are read for `name`,
`update_status` and `target_version`, which belong to their own documents.

#### 4.2 Success

- **2.x never reads `code` on HTTP answers.** It judges success only by whether
  `data` has the expected content: a user object with `id`, a `token`, a
  `msgid`, and so on.
- **Measured 2026-09-28:** the token exchange answers `code` **1** with `msg`
  `Login successful` on success, and `code` **0** when refused (including the rate limit, §4.3).
- Anycubic's own Slicer Next UI treats **`code == 1`** as success on the
  send-order call (order 1001, camera). The camera answer's success message is
  `Operation successful`. It is not verified that 1 means success on every
  endpoint.
- An answer can be successful by `code` and `msg` and still lack the data you
  need. The camera-open order does this when another session holds the
  account (§5.2).

#### 4.3 Known messages and codes

| Seen on | `code` / `msg` | Meaning | Class |
|---|---|---|---|
| token exchange; also any call with a bad token | `msg` = `User does not exist` | the token is invalid in *any* way (§2.1). Not about the account. | credentials rejected |
| token exchange | `msg` = `Login information has expired. Please login again.` | the session behind the access token was revoked on the server (§5.1) | credentials rejected. A new token is needed. |
| userInfo | `data` null, or no `data.id` | the token was not accepted | credentials rejected |
| token exchange | `code` **0**, `msg` = `请求过于频繁。请稍后再试` ("requests too frequent, try again later"), no `data` | **rate limit.** Measured on 2026-09-28: a second exchange of the same access token within about 3 s of a successful one is refused like this; after 5 s or more it succeeds. **Not a credentials verdict.** Wait at least 5 s (10 s recommended) and try again. Never fall back to web mode and never ask for re-authentication because of it. 2.x treats it as a refusal, which can end in a false re-auth. | **transient** |
| userInfo, printer info | `msg` = `request error` | server maintenance or rate limiting (the only rate-limit signal known) | **transient** |
| send order | `data` null and `msg` = `No file found` | the cloud file does not exist | file-not-found error |
| send order | `data` null, any other `msg` | the order was refused. The `msg` is shown to the user. | action error |
| printer info / status | `code` **1007**, `msg` along the lines of "The printer has been deleted" / "printer not exist" | the printer left the account. Switching a printer to LAN Mode causes this, and turning LAN Mode off does not undo it. | not an auth failure: setup *not ready* (BEHAVIOUR §6 B8) |
| camera-open order | `msg` = `Operation successful` with the credentials block missing | another session holds the camera, or the printer has no camera | retry once after a fresh exchange (§5.2) |

#### 4.4 How 2.x classifies failures

| Condition | Config flow | Setup | Poll |
|---|---|---|---|
| Credentials rejected (rows 1–3 above, after every retry in §3.7) | `invalid_auth`, or `wrong_token_type` (§2.9) | re-authentication | re-authentication |
| Transport error, timeout, non-JSON body ("server maintenance?") | `cannot_connect` | 4 attempts 10 s apart, then terminal setup error | poll failed (transient; entities keep their state per BEHAVIOUR §5.5) |
| userInfo `request error` | `cannot_connect` | as above | poll failed |
| An answer without a `data` key, or with an unexpected shape (a read error) | `cannot_read_response` | not ready | poll failed |
| First printer record fails after the token was accepted (for example 1007) | `cannot_connect` on the printer step | not ready, with the LAN Mode advice | poll failed |
| Anything unclassified | `cannot_connect` | **not ready, never re-auth** (B6) | poll failed |

Required: only a credentials verdict from the server (after the retries and
fallbacks) may trigger re-authentication. Transport, parse, maintenance and
printer-record failures never do.

#### 4.5 Rate limiting

No HTTP 429 handling exists, and no rate-limit headers are known. The only
signal is `msg` = `request error`. Normal load is at most one userInfo plus one
printer-info and one project call per printer every 60 s (BEHAVIOUR §5.1).

---

### 5. Session behaviour

#### 5.1 Signing in elsewhere can displace the session

- Anycubic revokes cloud sessions on the server. On 2026-08-17 the maintainer's
  own **international** slicer token was revoked. The exchange then answered
  `Login information has expired. Please login again.` So this is not specific
  to China, where it was first reported (#13).
- The usual trigger is the same account signing in with Slicer Next or the phone
  app. It is **not guaranteed**: one user has run the slicer and Home Assistant
  side by side, mid-print, with no trouble. Treat displacement as normal,
  occasional Anycubic behaviour, not a fault.
- When the **user token** is displaced but the access token is still good,
  exchanging again (§2.6.5 step 5) recovers without the user doing anything.
  When the **access token** itself is refused, only a new pasted token helps
  (re-auth).
- One field report: a new, signature-valid token was refused over and over,
  reloading the entry did not help, and a full Home Assistant restart fixed it.
  The cause is not known.

#### 5.2 The camera follows the most recent session

- The cloud camera's credentials go to whichever session asked for them most
  recently. After a sign-in elsewhere, the camera-open order still answers
  `Operation successful` but without the credentials block. Every other call
  keeps working, so the userInfo check sees nothing wrong.
- Required: if the camera-open answer lacks credentials, force a fresh exchange
  once (drop the user token and exchange the access token again), then repeat
  the order. If it is still missing, report "no camera, or another Anycubic
  session holds the account". Never cache camera credentials: `client_uid` is
  issued fresh on every call.

#### 5.3 The token store must never override a newer pasted token (required behaviour)

**The 2.x bug (fixed in 2.1.1, B7):** setup laid the stored tokens over the
entry's token. Once the stored pair had been revoked it was loaded again on
every retry, including straight after a successful re-auth. So a newly pasted,
provably good token was overwritten about a second later. The entry stayed on
"Authentication failed" with working credentials inside it, and the only way
out was deleting the entry, which loses every entity id and its history.

Required rules:

1. **The pasted token on the entry is authoritative. The store is a cache** of
   what was derived from it: the user token from the exchange, the device id,
   and the mode after any fallback.
2. **Per-entry store** `anycubic_cloud.<entry id>`, so two accounts never
   overwrite each other. If it is empty, read the legacy bare `anycubic_cloud`
   store and copy it to the per-entry key.
3. **At setup**, overlay the store on the entry (§2.6.5 step 3). If the cloud
   refuses the resulting credentials **and a store was used**, retry once with
   **only** the entry's token and mode. A success overwrites the store.
4. **A new paste** (re-authentication through the `cloud` form) must, in this
   order: write the new token, mode, region and device id to the entry;
   **delete the token store**; **schedule a reload** of the entry; then finish
   with `reauth_successful`. A re-auth without the reload leaves the entry in
   its old error state until something else reloads it.
5. **Save** the store after every successful setup, and whenever a poll changed
   the tokens.
6. Reconfigure → *printer* uses the entry's token **with** its store (it is not
   a re-auth). Re-auth and first setup never read the store.
7. An unclassified error is never an authentication failure (§4.4).

Optional hardening (a spec-team suggestion, not 2.x behaviour): treat the store
as stale and ignore it when it plainly belongs to a different pasted token. For
example: a SLICER store whose `auth_access_token` differs from the entry's
`user_token`, or a WEB or ANDROID store whose `auth_token` differs from it.
Rules 3–4 must still be implemented. The hardening only adds a safety net.

Diagnosing in the field: the store is what setup actually uses. Compare the
length of the stored `auth_access_token` (or `auth_token`) with the entry's
`user_token`. If they differ, the store is overriding the entry.

---

### 6. Open points

1. **WEB run-time discovery**: which page would be fetched, and how the two
   identical 32-character minified patterns would tell the app id from the app
   secret. It cannot be determined, because 2.x never runs discovery. Fixed
   constants are what work today.
2. **`GET /v3/public/getoauthToken`** and the app redirect URI: purpose,
   parameters and response are unknown. Both are unused.
3. **Success `code`**: 2.x never reads it. `1` is taken from Anycubic's UI for
   one call only. Not verified for other endpoints, and error codes other than
   1007 are not recorded.
4. **Server-side checks** not established: the timestamp skew window; whether
   nonces are checked for uniqueness or format (the Android encoding can only
   produce 32 of 62 characters); what `Xx-Is-Cn` means (it is `1` for the
   international web and slicer modes); whether `User-Agent`, `Origin`,
   `XX-LANGUAGE` or `Content-Type` on a GET are required; whether the server
   ties a user token to `Xx-Device-Type`. 2.x relies on the mixed header set
   after the web fallback being accepted.
5. **China**: the base, auth and MQTT hosts come from one user's report. The
   port, API path and image base are assumed. `Xx-Is-Cn` is not varied by
   region in 2.x and may need to be for China. The kind of China slicer token
   (HS512 user token or Casdoor token), and whether China has an exchange at
   all, are not confirmed. The config-flow JWKS check always uses the
   international URL.
6. **Client ids** are never sent. It is not known whether any endpoint needs
   them.
7. **Android**: how the app obtains its user token, the format of its device
   id, and whether the server ties the token to the device id. None of this was
   recorded. The 33-hex made-up id is 2.x's own invention.
8. **Lifetimes**: how long a user token from the exchange lives, and whether web
   user tokens carry `exp` and last 90 days. Only the slicer access token's
   90 days is verified.
9. **Displacement**: which sign-ins displace the Home Assistant session, and why
   it happens only sometimes. Also the "restart fixed it" case in §5.1.
10. **HTTP status codes**: never recorded, because 2.x ignores them. It is not
    known whether errors come back as non-200 with a JSON envelope, or as 200.
11. **Cookies**: whether the workbench API sets any, and whether they matter.
12. **Mixed-mode entries** (§2.8): what 3.0 should record when a SLICER attempt
    succeeds through the web fallback. 2.x keeps 3 on the entry and 1 in the
    store. Compatibility needs 3.0 to *accept* both. Whether 3.0 should rebuild
    WEB headers after a fallback is a decision to make.

---

## Part B. Cloud: HTTP API endpoint catalogue

Specification-team facts document for the clean-room 3.0 rewrite. Describes
every Anycubic cloud HTTP endpoint that the GPL library defines or calls, the
exact request and response shapes, and when the 2.x integration calls each.
Contains no code.

| Item | Value |
|---|---|
| Sources read | library `anycubic-cloud-api` **0.4.32** (commit `4ddf7b6`), its tests; integration `hass-anycubic` **2.9.4** (commit `f180ffd`), its tests and the captured fixture `tests/fixtures/printer_kobra_s1.json` |
| Companion documents | `BEHAVIOUR.md` (entity meanings; cited **BEH §n**), `COMPAT.md` (identifiers; cited **COMPAT §n**), the auth/signing section of this spec set (cited **AUTH**), the cloud-MQTT section (cited **CMQTT**), `anycubic-lan` `docs/PROTOCOL.md` (cited **LAN §n**) |
| Not covered here | request signing and header set (AUTH), token login internals (AUTH), MQTT topics and report payloads (CMQTT), the printer-local HTTP/MQTT (LAN), the Agora camera signalling (BEH §9 V8) |
| Secrets rule | Credential values are never written; they are named by their `CloudSecrets` field (INTEGRATION-SPEC §2). Personal data in examples is replaced by `<placeholders>`. |

---

### 0. Conventions and request mechanics

#### 0.1 Building a URL

| Part | Rule |
|---|---|
| Region base | `https://<base domain>/`, where the base domain is the region's (the base domain for the international region; the China region has its own — see the regions section). The base always ends in exactly one `/`. |
| API root | the region base followed by the constant the API path (`p/p/workbench/api`) (a relative path with **no** leading slash). Called **`{API}`** below. Both regions use the same the API path (`p/p/workbench/api`) in 2.x (unconfirmed for China). |
| Endpoint path | every path below starts with `/` and is appended to `{API}`. There is exactly one `/` at each seam; a doubled or missing slash is a silent failure (reported by users as "server maintenance", see §0.3). |
| Case | paths are used exactly as written. Two contain capitals: `/v2/Printer/status` and `/work/printer/Info`. They are distinct from `/v2/printer/info`. |
| Exceptions | the cloud-storage upload PUT (§4.4 step 4) goes to a pre-signed URL returned by the server, and job/printer images are fetched from absolute URLs (§3.6). Neither uses `{API}`. |

#### 0.2 Sending a request

| Aspect | Rule |
|---|---|
| Methods | GET, POST; PUT only for the storage upload. |
| GET parameters | sent in the query string. The library stringifies ids (`"12345"`); integers such as `limit` are sent as their decimal text. |
| POST body | always a JSON document, `Content-Type: application/json`. A POST with no parameters still sends the body `{}`. POSTs carry no query string. |
| Headers | signed as in the auth section (AUTH). Every call carries the user token **except** the access-token login (§1.1). In the *web* auth mode a browser User-Agent and an `Origin` of the region's auth domain are added (AUTH). |
| Cookies | a per-entry cookie jar is kept; nothing in the library reads cookies. |
| Timeouts / retries | the library sets no timeout of its own and has no generic retry. The specific retries that exist are listed with each endpoint (§1.1, §4.5) and in §8. |
| Slow calls | a call taking more than **20 s** logs a warning, at most once per **10 min**. |

#### 0.3 Response envelope

Every `{API}` response is a JSON object with at least:

| Field | Type | Meaning |
|---|---|---|
| `code` | integer | server status. **Never inspected by the library for HTTP.** Values seen in 2.x reports: `1007` = printer deleted (what the cloud answers for a printer that switched to LAN Mode). The success value is not asserted anywhere in the sources (see Open points). |
| `msg` | string | human text; the library keys some decisions on it (table below). |
| `data` | object, array, string or null | the payload. Absent/null usually means failure. |

Known `msg` texts and how 2.x reacts:

| `msg` | Where seen | Meaning / 2.x handling |
|---|---|---|
| `request error` | user info (§1.3); printer detail (§2.3) | user info: treated as "server maintenance"; printer detail: treated as "rate limited" (only when parsing the reply also failed) |
| `No file found` | send order, start print (§5) | the cloud does not (yet) know the file; start-print is retried (§4.5) |
| `Operation successful` | send order | returned for accepted orders — **including orders the printer silently ignores** (wrong `order_id` type, stray `project_id`, missing camera flag; see §5.1) |
| `Print task does not exist` | send order 6 (print settings) | no job is running; the settings order is refused |
| `User does not exist` | access-token login (§1.1) | the token is not a slicer token or is expired/stale (AUTH) |
| `Video service upgraded. Update the slicer to enable.` | send order 1001 | the camera flag was missing (§5.4.14) |

Transport failures, a non-JSON body (for example an HTML error page) and any
exception while reading the body are all reported by the library as one
generic "unexpected response, server maintenance?" error. HTTP status codes
are never inspected.

#### 0.4 Typing conventions of the server

- Numbers frequently arrive as strings and strings as numbers; ids are
  integers in responses but are sent back as strings in most query strings.
- JSON **inside strings**: a job's `settings`, `slice_param` and
  `slice_result` may be a JSON-encoded string **or** an object (§3.1).
- Present-but-null is common and differs from absent (§10). A default for an
  absent key does not protect against a null value.
- Empty string is used for "no value" in several text fields (`sku`, `type`,
  `update_status`, `thumbnail`).

#### 0.5 Endpoint index

"2.x" = does the integration call it. "When" is detailed in §8.

| # | Name | Method | Path (after `{API}`) | 2.x | When |
|---|---|---|---|---|---|
| E1 | Access-token login | POST | `/v3/public/loginWithAccessToken` | yes | setup; any poll after the cached user token was dropped (slicer tokens only) |
| E2 | Web OAuth token | GET | `/v3/public/getoauthToken` | **no** (defined only) | — |
| E3 | User info | GET | `/user/profile/userInfo` | yes | config/options flow, setup, **every cloud poll**, diagnostics |
| E4 | My printers | GET | `/work/printer/getPrinters` | yes | config flow (printer picker), options flow, diagnostics |
| E5 | Printers status | GET | `/work/printer/printersStatus` | no | — |
| E6 | Printer detail | GET | `/v2/printer/info` | yes | config flow, setup, **every cloud poll per printer**, home-all wait, diagnostics |
| E7 | Printer status (single) | GET | `/v2/Printer/status` | no (debug only) | — |
| E8 | Printer model catalogue | GET | `/v2/printer/all` | no | — |
| E9 | Printer tools | GET | `/v2/printer/tool` | no (defined only) | — |
| E10 | Printer functions | GET | `/v2/printer/functions` | no (defined only) | — |
| E11 | Rename printer | POST | `/work/printer/Info` | no (library only) | — |
| E12 | Job list | GET | `/work/project/getProjects` | yes | **every cloud poll per printer**, diagnostics |
| E13 | Job detail | GET | `/v2/project/info` | yes | every cloud poll per printer (for the latest job), diagnostics |
| E14 | Print history | GET | `/v2/project/printHistory` | no (debug only) | — |
| E15 | Job monitor | GET | `/v2/project/monitor` | no (debug only) | — |
| E16 | Sliced-file detail (FDM) | GET | `/work/gcode/infoFdm` | yes | print-and-upload *save in cloud* action |
| E17 | Cloud file list | POST | `/work/index/files` | yes | button *request cloud file list*; 5 s after a cloud delete; during *save in cloud* |
| E18 | Delete cloud files | POST | `/work/index/delFiles` | yes | action *delete cloud file* |
| E19 | Cloud storage quota | POST | `/work/index/getUserStore` | yes | before and after a *save in cloud* upload |
| E20 | Lock storage space | POST | `/v2/cloud_storage/lockStorageSpace` | yes | every upload |
| E21 | Register uploaded file | POST | `/v2/profile/newUploadFile` | yes | every upload |
| E22 | Unlock storage space | POST | `/v2/cloud_storage/unlockStorageSpace` | yes | every successful upload |
| E23 | Send order | POST | `/work/operation/sendOrder` | yes | every printer control that is not diverted to LAN; camera open |
| E24 | Printer firmware update | GET | `/work/printer/update_version` | yes | update entity *install* (printer) |
| E25 | ACE firmware update | POST | `/v2/printer/update_multi_color_box_version` | yes | update entity *install* (ACE 1 / ACE 2) |
| X1 | Storage upload | PUT | pre-signed URL from E20 | yes | every upload |
| X2 | Image fetch | GET | absolute URL (§3.6) | yes (image entity) | when the job image URL changes |

---

### 1. Account and user

#### 1.1 E1 — Access-token login (`POST /v3/public/loginWithAccessToken`)

Exchanges a slicer access token for a user token. Full detail belongs to AUTH;
the HTTP facts are:

| Aspect | Value |
|---|---|
| Token header | **not** sent (the only call without it) |
| Body | `device_type` (string: `android`, `pcf` for the slicer, `web` otherwise), `access_token` (string, the pasted token) |
| Response `data` | object; `data.token` (string) = the user token used on every later call |
| Failure | `data` null/empty; `msg` carries the reason (e.g. `User does not exist`) |
| Retries | up to **2** attempts, **2 s** apart. If both fail, the token is re-tried as a plain web user token (auth mode becomes *web*) before the credentials are declared bad. |
| When (2.x) | only when the entry holds a slicer token and no user token is cached: at setup and on the first poll after a failed user-info check forced the cached token to be dropped (also before a camera-open retry, §5.4.14). |

#### 1.2 E2 — Web OAuth token (`GET /v3/public/getoauthToken`)

Defined in the endpoint table, never called by this library version or by
2.x. Parameters and response unknown from these sources (AUTH may know).

#### 1.3 E3 — User info (`GET /user/profile/userInfo`)

| Aspect | Value |
|---|---|
| Query | none |
| Purpose | the token check. A reply carrying a user id means the credentials are good. |
| When (2.x) | config flow and options flow (token check), setup, **start of every cloud poll** (§8.2), diagnostics (raw). |

Response `data` (object):

| Field | Type | Meaning / use |
|---|---|---|
| `id` | integer | account user id. **Required and non-null**; 2.x uses it as the config entry's unique id (COMPAT §1). |
| `user_email` | string or null | account e-mail; part of the user identifier |
| `mobile` | string or null | phone number; fallback identifier |
| `user_nickname`, `birthday`, `password`, `message_key`, `last_login_ip`, `last_login_time`, `create_time`, `create_day_time`, `casdoor_user_id`, `casdoor_user`, `ip_country`, `ip_province`, `ip_city` | various | present (2.x diagnostics redacts exactly these names); not used |

The "user identifier" the library keeps is the e-mail, else the mobile, else
the id as text.

Failure rules (in this order):

1. `msg` is `request error` → "server maintenance" error (setup retries, §8.1).
2. `data` null → invalid credentials.
3. `data` present but `data.id` null → invalid credentials (a rejected token
   can still return a data object).

On invalid credentials with a slicer token and a cached user token, the cached
user token is dropped and the whole check (E1 then E3) runs once more.

---

### 2. Printers

#### 2.1 E4 — My printers (`GET /work/printer/getPrinters`)

| Aspect | Value |
|---|---|
| Query | none |
| Response `data` | **array** of printer records (one per printer on the account). Empty array when the account has no printers — also what an account shows while its only printer is in LAN Mode. |
| When (2.x) | config flow printer step (builds the id → name picker; parse faults tolerated), options flow (to decide whether to offer drying presets: any printer with function id 2006, §2.3.9), diagnostics (raw). |

Printer record fields (list form). "Req" = the library indexes the key
directly, so its absence fails the parse; a clean implementation should
tolerate absence.

| Field | Type | Req | Meaning |
|---|---|---|---|
| `id` | integer | yes | cloud printer id (used everywhere as `printer_id`) |
| `user_id` | integer | yes | owner account id |
| `name` | string | yes | user-given printer name |
| `nonce` | string/int | yes | per-printer value; not used by 2.x (treat as sensitive) |
| `key` | string | yes | printer key (appears in CMQTT topics; **sensitive**) |
| `machine_type` | integer | yes | numeric model id (e.g. `20025` Kobra S1, `20030` Kobra X) |
| `model` | string | yes | model name |
| `img` | string (URL) | no | model picture |
| `description` | string | no | in the detail form this carries a serial-like text |
| `type` | string | yes | printer type text (meaning not established; see Open points) |
| `device_status` | integer | yes | 1 online, 2 offline (BEH §1.1) |
| `ready_status` | integer | yes | not used |
| `is_printing` | integer | yes | 1 free, 2 busy (BEH §1.1); absent → treated as 1 |
| `reason`, `video_taskid`, `msg`, `status`, `delete`, `delete_time`, `last_update_time`, `create_time`, `available` | various | `status`, `delete`, `delete_time`, `last_update_time` yes | carried, not used by entities |
| `material_used` | string | no | lifetime filament text, e.g. `"18.17kg"` (top level here, under `base` in E6) |
| `print_totaltime` | string | no | lifetime print time text, e.g. `"798hour29min"` (top level here, under `base` in E6) |
| `machine_mac` | string | no | MAC address (**sensitive**; COMPAT keys devices by it) |
| `machine_data` | object or null | yes | §2.3.3 |
| `type_function_ids` | array of integers | yes | §2.3.9 |
| `material_type` | string | no | `Filament` / `Resin` (top level here, under `base` in E6) |
| `parameter` | object | no | §2.3.4 |
| `version` | object | yes | printer firmware, §2.3.5 |
| `color` | array of `[r,g,b]` arrays | no | colour list. Only the first 4 entries are meaningful unless a later entry contains a component other than `-2` (`-2` = unused filler). Purpose not established; not used by entities. |

#### 2.2 E5 — Printers status (`GET /work/printer/printersStatus`)

No query. `data` is an array of printer records of the **same shape as E4**.
Defined in the library, not called by 2.x.

#### 2.3 E6 — Printer detail (`GET /v2/printer/info`)

The main polled record. Supplies online/busy state, current temperatures,
firmware, ACE and external-holder state, capabilities and lifetime counters.

| Aspect | Value |
|---|---|
| Query | `id` = printer id, as a string |
| Response `data` | object (below). For a printer the cloud has deleted (LAN Mode) the reply has `code` 1007 and no usable `data`. |
| When (2.x) | config flow (validates each selected printer; parse faults tolerated), setup (once for the first printer as a reachability check, then once per printer to build it), **every cloud poll, per printer** (§8.2), every **2 s** for up to **45 s** during *home all* (§8.4), when a newly selected printer is picked up, diagnostics (raw). |
| Refresh-path quirk (2.x) | when refreshing an existing printer, a null `data` or a `data.id` that does not match is **silently ignored** (the printer keeps its previous values and the poll counts as a success). |
| Rate limit | if the reply cannot be parsed and `msg` is `request error`, 2.x reports "rate limited?". |

##### 2.3.1 Top-level fields

| Field | Type | Used for (2.x) |
|---|---|---|
| `id` | integer | identity; must match the requested id |
| `name` | string | printer name |
| `key` | string | printer key (sensitive) |
| `machine_type` | integer | model id; attribute on `current_status` (BEH §2.1) |
| `model` | string | model name (`current_status` attribute `model`, device model) |
| `img` | string URL | model picture (not an entity) |
| `device_status` | integer | `printer_online` (1 online, 2 offline) |
| `is_printing` | integer | `is_available` (1) / `is_busy` (2) / `current_status` |
| `base` | object | §2.3.2 |
| `machine_data` | object | §2.3.3 |
| `parameter` | object | §2.3.4 — `curr_nozzle_temp`, `curr_hotbed_temp` sensors |
| `type_function_ids` | array of integers | §2.3.9 — capability gating |
| `version` | object | §2.3.5 — `fw_version` update entity |
| `multi_color_box_version` | array | §2.3.6 — ACE update entities |
| `tools` | array | §2.3.7 — parsed, not surfaced |
| `external_shelves` | object | §2.3.8 — `external_spool_*` (BEH §2.7) |
| `multi_color_box` | object **or** array | §2.3.10 — every ACE entity (BEH §2.8–2.9) |
| `temp_limit` | object | `hotbed_temp_limit` `[min,max]`, `nozzle_temp_limit` `[min,max]` in °C for printing. **Ignored by the library.** |
| `free_temp_limit` | object | same shape; limits for idle preheating (observed `[0,110]` bed, `[0,320]` nozzle). **Ignored by the library.** |
| `features` | array of `{name, value}` | named booleans, see §2.3.11. **Ignored by the library** on this path. |
| `max_box_num` | integer | maximum ACE units the printer supports (observed 4). Ignored. |
| `nozzle_diameter` | number | mm. Ignored. |
| `head_tools_model`, `rotate_deg`, `is_queue_task`, `need_update`, `advance`, `help_url`, `quick_start_url`, `is_read_quick_start_url`, `maintenance_manual_url`, `releasefilm_url` | various | ignored |

##### 2.3.2 `base`

| Field | Type | Meaning / 2.x use |
|---|---|---|
| `print_count` | integer | lifetime prints → `print_count_total` |
| `print_totaltime` | string | lifetime print time → `print_time_total_hrs`. Two formats: a plain number = **minutes** (may be fractional), or `<h>hour<m>min` (whole string, digits only, no spaces). Anything else (including a JSON number rather than a string) reads as zero in 2.x. |
| `material_used` | string | lifetime filament, e.g. `"18.17kg"` → `material_used_total` (kg; only a number followed by `kg` is accepted, case-insensitive) |
| `material_type` | string | `Filament` / `Resin` (title-cased by the client) → which entity families exist (BEH §1.7) |
| `description` | string | serial-like text (sensitive) |
| `create_time` | integer | unix seconds |
| `firmware_version` | string | same as `version.firmware_version`; ignored |
| `machine_mac` | string | MAC address (sensitive) → device identity |

##### 2.3.3 `machine_data`

All ten keys are required together when the object is present (a null inside
fails the parse in 2.x).

| Field | Type | Meaning |
|---|---|---|
| `name` | string | model name |
| `pixel` | number | resin pixel size (µm); filler on FDM |
| `res_x`, `res_y` | integer | resin screen resolution; filler on FDM |
| `format` | string | resin preview format; filler on FDM |
| `size_x`, `size_y`, `size_z` | number | build volume, mm |
| `suffix` | string | printable file extension (`gcode` on FDM) |
| `anti_max` | integer | maximum anti-aliasing level |

Not surfaced as entities in 2.x.

##### 2.3.4 `parameter`

| Field | Type | Meaning |
|---|---|---|
| `curr_hotbed_temp` | integer | bed temperature now, °C → `curr_hotbed_temp` |
| `curr_nozzle_temp` | integer | nozzle temperature now, °C → `curr_nozzle_temp` |

Both required when the object is present.

##### 2.3.5 `version` (printer firmware)

| Field | Type | Meaning |
|---|---|---|
| `need_update` | integer | **1 = an update is available**; anything else = none. Required. |
| `firmware_version` | string | installed version. Required. |
| `target_version` | string | latest version. **When no update is available it equals the installed version** (captured: `2.7.2.7` / `2.7.2.7` with `need_update` 0) — answers BEH §9 V17. |
| `update_progress` | integer | 0–100 (0 when idle) |
| `update_status` | string | empty when idle |
| `update_date` | integer | 0 when never |
| `update_desc` | string | release notes (free text, may contain `\n`) |
| `force_update` | integer (the library stores it as text) | 0/1 |
| `time_cost` | integer | expected duration (unit not established) |
| `box_id` | integer | only on ACE entries (§2.3.6) |
| `img` | string URL | picture; ignored |

Update entity (BEH §2.17): installed = `firmware_version`, latest =
`target_version`, install allowed only when `need_update` is 1 (§6).

##### 2.3.6 `multi_color_box_version` (ACE firmware)

Array, one entry per ACE, same fields as §2.3.5 plus `box_name` (e.g.
`"ACE Pro"`) and `box_id` (integer, 0-based). 2.x uses entry 0 for ACE 1 and
entry 1 for ACE 2, by array position. On a refresh, an empty or non-array
value is ignored (the previous values stay).

##### 2.3.7 `tools`

Array of function descriptors. Each has 13 keys: `id`, `typd_id` (sic),
`model_id`, `type_function_id`, `parent_id`, `function_name`, `function_des`,
`control`, `param` (array), `icon_url`, `function_type`, `status`,
`show_place`. Nulls in any of them are tolerated. Descriptive only; 2.x does
not surface them.

##### 2.3.8 `external_shelves` (single-spool external holder)

| Field | Type | Meaning |
|---|---|---|
| `id` | integer or null | holder id; null when there is no holder |
| `type` | string | material in the holder (`""` when none) |
| `color` | array `[r,g,b]` | colour; may contain nulls (dropped by the client) |
| `loaded` | integer or null | 1 = spool loaded |
| `status_type`, `current_status` | integer | status codes (observed −1, 2, 11); not interpreted |
| `brand_name`, `material_name` | string or null | seen in firmware 2.0.1.9 payloads; not used |

**Presence rule:** firmware 2.0.1.9 sends this object whether or not a holder
exists, all-null when absent. The holder counts as **absent** when `id` is
null **and** `type` is empty/null **and** `loaded` is null. Otherwise it is
present even if other fields are null.

Captured (firmware 2.0.1.9, printer with an ACE and no holder — hass-anycubic
#28):

```json
{
  "brand_name": null,
  "color": [255, 255, 255],
  "current_status": 11,
  "id": null,
  "loaded": null,
  "material_name": null,
  "status_type": 2,
  "type": ""
}
```

##### 2.3.9 `type_function_ids` (capabilities)

Array of integer function ids. 2.x tests membership. The names below are **compatibility data**: 2.x shows exactly these strings in the `supported_functions` attribute (BEH §2.14), so 3.0 must produce them unchanged.

| Id | Name | Id | Name |
|---|---|---|---|
| 1 | AXLE_MOVEMENT | 30 | NOVICE_GUIDE |
| 2 | FILE_MANAGER | 31 | RELEASE_FILM |
| 3 | EXPOSURE_TEST | 32 | TASK_MODE |
| 7 | LCD_PEER_VIDEO | 33 | LCD_INTELLIGENT_MATERIALS_BOX |
| 13 | FDM_AXIS_MOVE | 34 | LCD_AUTO_OUT_IN_MATERIALS |
| 22 | FDM_PEER_VIDEO | 35 | M7PRO_AUTOMATIC_OPERATION |
| 26 | DEVICE_STARTUP_SELF_TEST | 36 | AI_DETECTION |
| 27 | PRINT_STARTUP_SELF_TEST | 37 | AUTO_LEVELER |
| 28 | AUTOMATIC_OPERATION | 38 | VIBRATION_COMPENSATION |
| 29 | RESIDUE_CLEAN | 39 | TIME_LAPSE |
| 40 | VIDEO_LIGHT | 41 | BOX_LIGHT |
| 2006 | MULTI_COLOR_BOX (ACE supported) | | |

Captured Kobra S1 list: `2, 13, 22, 39, 41, 43, 44, 45, 47, 48, 2006`
(43–48 are not in the table; meaning unknown). The name list is what
BEH §2.14 exposes as `supported_functions`.

##### 2.3.10 `multi_color_box` (ACE units)

**Shape quirk:** a single ACE may arrive as a bare **object**; several arrive
as an **array**. Treat an object as a one-element array. The number of ACE
units = number of elements. A later report naming fewer boxes updates those
boxes and keeps the others (BEH §1.7).

Per box:

| Field | Type | Req | Meaning |
|---|---|---|---|
| `id` | integer | **yes** (a box without an id is rejected) | box id; used as `id` in ACE orders and in slot arithmetic (§5.4.2). See Open points on 0- vs 1-based. |
| `status` | integer | no (default 0) | box status code |
| `model_id` | integer | no | `40001` ACE Pro; `40002` seen elsewhere (BEH §1.9) |
| `auto_feed` | integer 0/1 | no | run-out refill switch |
| `loaded_slot` | integer | no (default −1) | slot feeding the printer, 0-based within the box; −1 none |
| `temp` | integer | no (default 0) | box temperature °C |
| `humidity` | number | no | %, reads 0 without a sensor |
| `feed_status` | object | no | `code` (200 = OK), `type`, `current_status`, `slot_index` (−1 none) |
| `drying_status` | object | no | `status` (1 drying), `target_temp` °C, `duration` min, `remain_time` min |
| `curr_nozzle_temp`, `target_nozzle_temp` | integer | no | nozzle temperatures as the box reports them |
| `slots` | array | no (default empty) | per slot, below |

Per slot:

| Field | Type | Meaning |
|---|---|---|
| `index` | integer | 0-based slot index within the box (0–3) |
| `sku` | string | filament SKU, `""` when typed by hand |
| `type` | string | material text (e.g. `PLA`, `PETG`) |
| `color` | `[r,g,b]` | colour |
| `color_group` | array of `[r,g,b,a]` | every colour on the spool; several entries = multi-colour spool |
| `status` | integer | 5 = loaded into the printer, 4 = not loaded |
| `edit_status` | integer | 0 read from tag, 1 typed by hand, **2 slot empty** |
| `icon_type` | integer | not interpreted |
| `consumables_percent` | number | reads 0 everywhere observed |

Null telemetry (`status`, `model_id`, `auto_feed`, `loaded_slot`, `temp`,
feed/drying sub-fields) is tolerated; entity meanings are in BEH §1.9 and
§2.8–2.9.

##### 2.3.11 `features`

Array of `{name: string, value: boolean}`. Names captured on a Kobra S1:
`auto_leveling_support`, `vibration_compensation_support`,
`flow_calibration_support`, `drying_first_support`,
`camera_timelapse_support`, `gcode_3mf_support`, `delete_batch_support`,
`preheating_support`, `fod_support`, `shengwang_rtc_support`,
`pre_cancel_support`, `shengwang_rdt_support`. The library ignores this list
on the HTTP path (it reads a `features` object only from LAN reports).

##### 2.3.12 Captured example (Kobra S1 with one ACE Pro, idle)

From `tests/fixtures/printer_kobra_s1.json` (the `data` object). Scrubbed;
arrays shortened where marked.

```json
{
  "is_queue_task": 0,
  "base": {
    "print_count": 174,
    "print_totaltime": "798hour29min",
    "material_type": "Filament",
    "material_used": "18.17kg",
    "description": "<printer_serial>",
    "create_time": 1751145530,
    "firmware_version": "2.7.2.7",
    "machine_mac": "<mac>"
  },
  "name": "Anycubic Kobra S1",
  "id": "<printer_id: integer>",
  "key": "<printer_key>",
  "img": "https://<cdn>/device/kobra_s1_<n>.png",
  "machine_type": 20025,
  "device_status": 1,
  "is_printing": 1,
  "model": "Anycubic Kobra S1",
  "free_temp_limit": { "hotbed_temp_limit": [0, 110], "nozzle_temp_limit": [0, 320] },
  "temp_limit": { "hotbed_temp_limit": [35, 120], "nozzle_temp_limit": [185, 320] },
  "machine_data": {
    "name": "Anycubic Kobra S1", "pixel": 34.4, "res_x": 11520, "res_y": 5120,
    "format": "pw0Img", "size_x": 250, "size_y": 250, "size_z": 260,
    "suffix": "gcode", "anti_max": 8
  },
  "rotate_deg": 0,
  "type_function_ids": [2, 13, 22, 39, 41, 43, 44, 45, 47, 48, 2006],
  "parameter": { "curr_hotbed_temp": 28, "curr_nozzle_temp": 31 },
  "nozzle_diameter": 0.4,
  "tools": [
    {
      "id": "<tool_id>", "typd_id": 2, "model_id": 20025, "type_function_id": 2,
      "parent_id": 0, "function_name": "Document Management",
      "function_des": "View or delete files stored locally on the printer",
      "control": 0, "param": [], "icon_url": "https://<cdn>/php/img/4/2.png",
      "function_type": 1, "status": 1, "show_place": 1
    }
  ],
  "advance": [],
  "help_url": "https://<wiki>/en/fdm-3d-printer/kobra-s1-Combo",
  "version": {
    "need_update": 0,
    "firmware_version": "2.7.2.7",
    "update_progress": 0,
    "update_date": 0,
    "update_status": "",
    "update_desc": "1.Fixed system errors ...\n2.Optimized key processes ...",
    "force_update": 0,
    "target_version": "2.7.2.7",
    "time_cost": 10,
    "img": "https://<cdn>/device/kobra_s1_<n>.png"
  },
  "quick_start_url": "https://<wiki>/...?_sasdk=<tracking>",
  "is_read_quick_start_url": 0,
  "maintenance_manual_url": "https://<wiki>/...",
  "multi_color_box_version": [
    {
      "box_name": "ACE Pro", "need_update": 0, "firmware_version": "1.3.863",
      "update_desc": "", "force_update": 0, "target_version": "1.3.863",
      "time_cost": 0, "update_progress": 0, "update_date": 0, "update_status": "",
      "img": "https://<cdn>/device/new_multi_color_box.png", "box_id": 0
    }
  ],
  "head_tools_model": 0,
  "external_shelves": {
    "type": "PLA", "color": [233, 157, 67], "loaded": 1, "id": "<shelf_id>",
    "status_type": -1, "current_status": -1
  },
  "need_update": 0,
  "releasefilm_url": "https://<wiki>/...",
  "multi_color_box": {
    "id": 1,
    "status": 1,
    "temp": 33,
    "humidity": 0,
    "model_id": 40001,
    "auto_feed": 1,
    "loaded_slot": -1,
    "feed_status": { "code": 200, "type": -1, "current_status": -1, "slot_index": -1 },
    "drying_status": { "status": 0, "duration": 0, "target_temp": 0, "remain_time": 0 },
    "curr_nozzle_temp": 31,
    "target_nozzle_temp": 0,
    "slots": [
      {
        "index": 0, "sku": "", "type": "PETG", "color": [175, 175, 175], "status": 5,
        "edit_status": 1, "color_group": [[175, 175, 175, 255]], "icon_type": 0,
        "consumables_percent": 0
      },
      {
        "index": 3, "sku": "<sku>", "type": "PETG", "color": [239, 240, 241], "status": 5,
        "edit_status": 0, "color_group": [[239, 240, 241, 255]], "icon_type": 0,
        "consumables_percent": 0
      }
    ]
  },
  "features": [
    { "name": "auto_leveling_support", "value": true },
    { "name": "camera_timelapse_support", "value": true },
    { "name": "shengwang_rtc_support", "value": true }
  ],
  "max_box_num": 4
}
```

(`slots` shortened from 4 entries, `tools` from 2, `features` from 12. In
the fixture the `tools[].id` and `external_shelves.id` had already been
replaced by test values, so their real type is not known from this capture.)

#### 2.4 E7 — Printer status (`GET /v2/Printer/status`)

Query `id` (printer id, string). The library only writes `data` to its debug
log. Not called by 2.x. Response shape unknown. Note the capital `P`.

#### 2.5 E8 — Printer model catalogue (`GET /v2/printer/all`)

No query. `data.printer_type` is an array of model entries:

| Field | Type | Meaning |
|---|---|---|
| `machine_type` | integer | model id |
| `name` | string | model name |
| `img` | string URL | picture |
| `net_function_ids` | array | network functions (not interpreted) |
| `net_default_function` | integer | not interpreted |

Library only; not called by 2.x.

#### 2.6 E9, E10 — Printer tools / functions (`GET /v2/printer/tool`, `GET /v2/printer/functions`)

Present in the endpoint table only; never called. Parameters and response
unknown.

#### 2.7 E11 — Rename printer (`POST /work/printer/Info`)

| Aspect | Value |
|---|---|
| Body | `id` (printer id **as a string**), `name` (string, non-empty) |
| Response `data` | object; `data.name` = the name now stored |
| Success rule | `data.name` equals the requested name. If it equals the **old** name the server reverted/refused; anything else is an unknown failure. |
| 2.x | not exposed. Note the capital `I` (different endpoint from E6). |

---

### 3. Jobs (the cloud calls them "projects")

#### 3.1 E12 — Job list (`GET /work/project/getProjects`)

| Aspect | Value |
|---|---|
| Query | `page` (string, always `"1"` in 2.x), `limit` (integer **2000** = the job-list limit), optional `print_status` (string status code filter; unused by 2.x) |
| Response `data` | **array** of job records, **newest first**, account-wide (all printers). `data` null → treated as an empty list. |
| When (2.x) | **every cloud poll, once per printer** (the list is fetched separately for each printer, not shared), and diagnostics (raw). |

Job record fields. The library indexes **all** of these directly (absence of
any key fails the whole list parse) — a clean implementation should tolerate
absence. Values arrive as integers or numeric strings.

| Field | Type | Meaning / 2.x use |
|---|---|---|
| `id` | integer | job id. This is the `project_id` sent in pause/resume/stop/settings orders and the key CMQTT print reports carry as task id. |
| `taskid` | integer | task id (distinct field; not used for matching in 2.x) |
| `user_id` | integer | owner |
| `printer_id` | integer | which printer ran it — used to pick this printer's latest job |
| `gcode_id` | integer | sliced file id |
| `model` | integer | not used (redacted in diagnostics) |
| `img` | string | preview picture: a full `http…` URL, or empty/other (see §3.6) |
| `estimate` | integer | slicer estimate (unit not established; see Open points) |
| `remain_time` | integer | minutes remaining → `job_time_remaining` |
| `print_time` | integer | minutes elapsed → `job_time_elapsed` |
| `progress` | integer | 0–100 → `job_progress` |
| `pause` | integer | non-zero = paused (BEH §1.3) |
| `print_status` | integer | job status code (BEH §1.3 table). Once a job object holds 2 or 3, a later poll does not overwrite it. |
| `reason` | string or 0 | failure text; `0` means none → `print_status_message` |
| `status` | integer **or null** | general status; **null seen** (China region, any account possible). 0 is a real value, distinct from null. |
| `create_time` | integer | unix s → `created_timestamp` |
| `start_time` | integer | unix s |
| `end_time` | integer | unix s; **> 0 once finished** → `job_eta` rule 1 and `finished_timestamp` (BEH §1.6) |
| `total_time` | string/number | total print time: number = minutes, or `<h>hour<m>min` → `print_total_time*` attributes |
| `gcode_name` | string | file name; the client strips a trailing `.gcode` → `job_name` |
| `settings` | JSON string or object | live job settings, §3.1.1 |
| `slice_param` | JSON string or object | slicer parameters, §3.1.2 |
| `slice_result` | JSON string or object | slice result, §3.1.3 |
| `source` | string | job origin → `job_name` attribute `source` |
| `material`, `material_type`, `connect_status`, `slice_data`, `slice_status`, `ischeck`, `project_type`, `printed`, `slice_start_time`, `slice_end_time`, `delete`, `auto_operation`, `monitor`, `last_update_time`, `localtask`, `device_message`, `signal_strength`, `post_title` | various | carried, not used by entities |
| `key` | string | printer key (**sensitive**) |
| `type` | string | printer type text |
| `machine_type` | integer | model id |
| `printer_name`, `machine_name` | string | names |
| `device_status` | integer | printer online code at job time |

An unparseable JSON string in `settings`, `slice_param` or `slice_result`
fails the whole list parse in 2.x.

##### 3.1.1 `settings` (decoded)

| Key | Type | Meaning |
|---|---|---|
| `curr_layer`, `total_layers` | integer | layer counters → `job_current_layer`, `job_total_layers` |
| `supplies_usage` | integer | filament extruded so far, mm (BEH §9 V1) → `job_filament_used` |
| `state` | string | the printer's text phase; used when `print_status` is an unknown code (BEH §1.3) |
| `slicer` | string | slicer name → `job_name` attribute |
| `model_hight` (sic) | number | resin model height, mm |
| `anti_count` | integer | resin anti-aliasing |
| `settings` | object | nested resin exposure block: `on_time`, `off_time`, `bottom_time` (s), `bottom_layers`, `z_up_height` (mm), `z_up_speed`, `z_down_speed` (BEH §2.11) |

A CMQTT print report later updates `curr_layer`, `total_layers` and
`supplies_usage` in place (CMQTT).

##### 3.1.2 `slice_param` (decoded)

| Key | Type | Meaning |
|---|---|---|
| `image_id` | string | relative path of the preview image (§3.6) |
| `paint_infos` | array | the job's colour list; each entry has at least `paint_index` (integer), `material_type` (string), `filament_used` (number, **grams** planned). Used for ACE slot mapping (§4.5) and the run-out forecast. |
| `printer_settings_id` | string | printer profile |
| `layer_height` | number | mm |
| `filament_type` | string | `;`-separated material list |
| `temperature`, `bed_temperature` | number | °C |
| `fill_density` | string/number | infill |
| `travel_speed` | number | mm/s |
| `brim_type` | string | — |

Values of −1 or `""` mean "not set by the slicer" and are dropped from the
`job_name` attributes (BEH §2.3).

##### 3.1.3 `slice_result` (decoded)

| Key | Type | Meaning |
|---|---|---|
| `size_x`, `size_y`, `size_z` | number | model size, mm |
| `used_filament` | number | estimated filament |

##### 3.1.4 Selecting "the latest job" for a printer (2.x)

1. Walk the list from the top (newest first).
2. The first record whose `printer_id` equals this printer becomes the
   latest job. If a job object already exists with the same `id`, it is
   updated in place (keeping its image if it already has one); otherwise it is
   replaced.
3. If that job has an image URL, or has no name, stop.
4. Otherwise keep walking the **following** records (any printer), at most
   **200** of them (the image-search limit); the first with the same
   name and an image URL lends its image, then stop.
5. If a latest job was found, call E13 for it.

No printer match → the printer has no job (BEH §1.3 "No job at all").

##### 3.1.5 Example (shape only — constructed from the field list; values illustrative)

```json
{
  "id": "<project_id: integer>",
  "taskid": "<task_id: integer>",
  "user_id": "<user_id: integer>",
  "printer_id": "<printer_id: integer>",
  "gcode_id": "<gcode_id: integer>",
  "model": 0,
  "img": "",
  "estimate": 12345,
  "remain_time": 42,
  "print_time": 95,
  "progress": 69,
  "pause": 0,
  "print_status": 1,
  "reason": 0,
  "status": 1,
  "create_time": 1790000000,
  "start_time": 1790000100,
  "end_time": 0,
  "total_time": "2hour17min",
  "gcode_name": "benchy_PLA_0.2.gcode",
  "settings": "{\"curr_layer\":120,\"total_layers\":240,\"supplies_usage\":31783,\"state\":\"printing\",\"slicer\":\"<slicer>\"}",
  "slice_param": "{\"image_id\":\"<relative/image/path.png>\",\"layer_height\":0.2,\"paint_infos\":[{\"paint_index\":0,\"material_type\":\"PLA\",\"filament_used\":94.5}]}",
  "slice_result": "{\"size_x\":60.0,\"size_y\":31.0,\"size_z\":48.0,\"used_filament\":94.5}",
  "source": "<source>",
  "key": "<printer_key>",
  "type": "<printer_type>",
  "machine_type": 20025,
  "printer_name": "<printer_name>",
  "machine_name": "Anycubic Kobra S1",
  "device_status": 1,
  "material": "", "material_type": 0, "connect_status": 1, "slice_data": null,
  "slice_status": 0, "ischeck": 0, "project_type": 1, "printed": 0,
  "slice_start_time": 0, "slice_end_time": 0, "delete": 0, "auto_operation": null,
  "monitor": null, "last_update_time": 1790005700, "localtask": null,
  "device_message": null, "signal_strength": 0, "post_title": null
}
```

#### 3.2 E13 — Job detail (`GET /v2/project/info`)

| Aspect | Value |
|---|---|
| Query | `id` = job id (E12 `id`), string |
| Response `data` | object merged into the latest job |
| When (2.x) | every cloud poll, per printer, for the latest job found in §3.1.4; diagnostics (for the account's newest job) |

Fields used:

| Field | Type | Meaning / 2.x use |
|---|---|---|
| `z_thick` | number | layer height mm → `job_z_thick` |
| `print_speed_mode` | integer | the job's speed-mode code (BEH §1.5) |
| `print_speed_pct` | integer | print speed % (fallback when the printer has not reported one) |
| `fan_speed_pct` | integer | the sliced job's fan % (not the live fan; BEH §2.2) |
| `task_mode` | integer | not interpreted |
| `reason_id` | integer | failure reason code; not surfaced |
| `type_function_ids` | array of integers | job-level function list; not used |
| `temp` | object | `target_nozzle_temp`, `target_hotbed_temp` (°C → `target_*_temp`), `limit.hotbed_temp_limit` `[min,max]`, `limit.nozzle_temp_limit` `[min,max]` (°C; used to validate print-settings changes, §5.4.3). A limit array that is not exactly 2 long is ignored. |
| `print_speed_model_des` | array of `{title, print_speed_mode}` | the modes this job/printer offers: `title` = display name, `print_speed_mode` = integer code. **This is the only source of speed-mode names** (`set_speed_mode` options, `job_speed_mode` text, `available_modes` attribute as `{description, mode}`). |

#### 3.3 E14 — Print history (`GET /v2/project/printHistory`)

No parameters sent. Library logs the raw reply only; not called by 2.x.
Parameters, pagination and shape unknown.

#### 3.4 E15 — Job monitor (`GET /v2/project/monitor`)

Query `id` (job id, string). Logged only; not called by 2.x. Shape unknown.

#### 3.5 E16 — Sliced-file detail, FDM (`GET /work/gcode/infoFdm`)

| Aspect | Value |
|---|---|
| Query | `id` = the **gcode id** (from E17 `gcode_id`), string |
| When (2.x) | during *print and upload — save in cloud* (§4.5 A), to read the file's colour list |

Response `data` (all keys required by the library):

| Field | Type | Meaning |
|---|---|---|
| `file_id` | integer | the **cloud file id** — this is what the start-print order sends as `file_id` |
| `gcode_id` | integer | echo of the requested id |
| `name` | string | file name |
| `size` | integer | bytes |
| `create_time` | integer | unix s |
| `estimate` | integer | estimate (unit not established) |
| `status`, `progress` | integer | processing state |
| `machine_class` | integer | not interpreted |
| `image_id` | string | preview (full URL or relative path, §3.6) |
| `slice_param` | JSON string or object | as §3.1.2; `paint_infos` = colour list |
| `slice_result` | JSON string or object | as §3.1.3 |

#### 3.6 Job images (X2)

The job preview URL is resolved as:

1. the job's `img` (E12) / `image_id` (E16) when it is a full URL starting
   with `http`; otherwise
2. the image URL base (the region's image base, which ends in `/`)
   followed by `slice_param.image_id`, when that is a non-empty string;
3. otherwise no image.

The image entity fetches the URL with a plain unsigned GET and caches it
until the URL changes (BEH §2.3). Model pictures (`img` on printer records)
and cloud-file `thumbnail` URLs are plain public URLs too.

---

### 4. Cloud files and storage

#### 4.1 E17 — Cloud file list (`POST /work/index/files`)

| Aspect | Value |
|---|---|
| Body | `page` (integer, 1-based), `limit` (integer, default **10**), optional `printable` (integer 0/1 — only printable/sliced files), optional `machine_type` (integer; 2.x only ever sends `0`, meaning not established) |
| Response `data` | array of file records, newest first; null/empty → no files |
| When (2.x) | button `request_file_list_cloud` (page 1, limit 10, no filters); 5 s after a successful cloud delete (same); *save in cloud* upload check (page 1, limit 10, `printable` 1, `machine_type` 0) |
| Pagination | 2.x never requests beyond page 1 — the entity shows at most the **10** newest files (BEH §2.5). |

File record — fields used (all others carried untouched):

| Field | Type | Meaning / 2.x attribute |
|---|---|---|
| `id` | integer | cloud file id (`id` in `file_info`; the id `delete_file_cloud` takes). Missing → the client uses −999. |
| `old_filename` | string | the user's file name → `name` |
| `filename` | string | server-side stored name (not shown) |
| `size` | integer | bytes → `size_mb` (bytes ÷ 1 000 000) |
| `thumbnail` | string URL | rendered preview; `""` = none → `thumbnail` |
| `estimate` | integer | estimated print time, **seconds** → `estimate_seconds` |
| `material_name` | string | → `material` (`""` = none) |
| `layer_height` | number | mm → `layer_height` |
| `supplies_usage` | integer | planned filament, **mm** → `filament_mm` |
| `size_x`, `size_y`, `size_z` | number | model size mm → `dimensions` `{x,y,z}` (null when `size_x` is null) |
| `gcode_id` | integer or null | sliced-file id for E16; **null until the cloud has parsed the file** |
| `is_temp_file` | integer | 1 for upload-and-print-without-saving files |
| `user_lock_space_id` | integer | the lock id it was uploaded under (§4.4) |

Other fields present: `user_id`, `post_id`, `time`, `status`, `ip`,
`img_status`, `device_type`, `file_type`, `md5`, `url`, `is_delete`,
`update_time`, `uuid`, `store_type`, `bucket`, `region`, `path`,
`thumbnail_nonce`, `sliceparse_nonce`, `file_extension`, `name_counts`,
`source_user_upload_id`, `origin_post_id`, `stl_user_upload_id`,
`is_official_slice`, `triangles_count`, `is_parse`, `source_type`,
`file_source`, `origin_file_md5`, `official_file_key`, `official_file_id`,
`simplify_model`, `printer_names`, `slice_param`. (`url`, `bucket`, `path`
and nonces may be signed or storage-internal — treat as sensitive.)

#### 4.2 E18 — Delete cloud files (`POST /work/index/delFiles`)

| Aspect | Value |
|---|---|
| Body | `idArr`: array of cloud file ids (integers). 2.x always sends exactly one. |
| Success | `data` is the **empty string** `""`. Anything else = failure. |
| When (2.x) | action `delete_file_cloud` (BEH §4.5); on success the list (E17) is re-fetched 5 s later. |

#### 4.3 E19 — Cloud storage quota (`POST /work/index/getUserStore`)

| Aspect | Value |
|---|---|
| Body | `{}` |
| Response `data` | `used_bytes` (integer), `total_bytes` (integer), `used` (string, human text), `total` (string, human text), `user_file_exists` (boolean). All required by the library. |
| Derived | available = `total_bytes − used_bytes` |
| When (2.x) | only inside a *save in cloud* upload, before and after (§4.4). Not exposed as an entity. |

#### 4.4 Upload flow (E19 → E20 → X1 → E21 → E22 → E19)

Steps, in order. "Temp" = upload for print-without-saving (`is_temp_file` 1).

| Step | Call | Details |
|---|---|---|
| 1 | read the file | whole file in memory; size = byte length; empty file or zero size → error |
| 2 | E19 quota | **skipped for temp.** Fails if available < file size. |
| 3 | E20 `POST /v2/cloud_storage/lockStorageSpace` | body: `size` (integer, bytes), `name` (string, file name without directories), `is_temp_file` (integer 0/1). Response `data`: `id` (integer — the **lock id**), `preSignUrl` (string — pre-signed storage PUT URL, **sensitive**). |
| 4 | X1 `PUT <preSignUrl>` | body = the raw file bytes. **No signed headers and no token** (the URL carries its own authorisation). The library sets no Content-Type itself (its HTTP client's default for raw bytes applies; see Open points). In the *web* auth mode only, a browser User-Agent and an `Origin` header are also sent. **Success = empty response body**; any non-empty body is the storage error text and fails the upload. The HTTP status is not checked. |
| 5 | E21 `POST /v2/profile/newUploadFile` | body: `user_lock_space_id` (integer, the lock id). Response `data.id` (integer) = the new **cloud file id**. Missing `data` or `data.id` → "file claim failed". |
| 6 | E22 `POST /v2/cloud_storage/unlockStorageSpace` | body: `id` (integer, lock id), `is_delete_cos` (integer: 1 = discard the stored object because the upload failed, 0 = keep). Response ignored. |
| 7 | E19 quota again | **skipped for temp.** Fails unless available bytes dropped by at least the file size ("uploaded file not found in cloud"). |

Result: the cloud file id from step 5.

Quirk: when step 4 or 5 fails, 2.x raises **before** step 6, so the unlock
with `is_delete_cos` 1 is never actually sent; the lock is left behind.

#### 4.5 Starting a print from an upload

**A. Save in cloud** (action `print_and_upload_save_in_cloud`):

1. Upload with `is_temp_file` 0 (§4.4) → cloud file id *F*.
2. E17 with page 1, limit 10, `printable` 1, `machine_type` 0; take the
   first record. Its `id` must equal *F* ("upload mismatch" otherwise) and its
   `gcode_id` must be non-null ("no gcode id" otherwise — a race if the cloud
   has not parsed the file yet).
3. E16 with that `gcode_id` → `file_id` and `slice_param.paint_infos`.
4. If the printer has an ACE: the slot list length must equal the number of
   `paint_infos` entries; build the slot mapping (§5.4.2).
5. Send order 1 (§5.4.1) with `file_id` = E16 `file_id`, `is_delete_file` 0.
   Retried up to **3** times, **3 s** apart, while the reply is
   `No file found`.

**B. No cloud save** (action `print_and_upload_no_cloud_save`):

1. If the printer has an ACE: read the colour list **from the G-code header**
   (not from the cloud) and build the slot mapping; file name must end
   `.gcode`. (Header parsing is outside this document.)
2. Upload with `is_temp_file` 1 (no quota checks) → cloud file id *F*.
3. Send order 1 with `file_id` = *F* and `is_delete_file` **1** (the cloud
   deletes it after printing). Same retry rule.

**C. Print a file already on the printer** (`print_local_file`): no upload;
order 1 in its *local* form (§5.4.1).

Library-only (not exposed by 2.x): print an existing cloud file by cloud file
id, or by gcode id (steps A3–A5 alone).

Slot-list rule common to A and B: a printer **with** an ACE requires a slot
list; a printer **without** one must not be given one.

---

### 5. Printer orders over HTTP — E23 `POST /work/operation/sendOrder`

Every printer control goes through this one endpoint. Replies to queries
arrive asynchronously over CMQTT, not in the HTTP response (except camera
open).

When a LAN connection to the printer is up, an order that has a local form
(the "LAN form" column of §5.3) is sent over LAN **instead** and never
reaches HTTP (local message shapes: LAN PROTOCOL). The camera-open call is
never diverted.

#### 5.1 Body shapes

The server accepts every shape and answers `Operation successful`, but the
**printer silently ignores orders sent in the wrong shape**. The shapes are:

| Shape | `order_id` | `printer_id` | `project_id` | `data` | Extra keys | Used for |
|---|---|---|---|---|---|---|
| **P** printer-level | **string** | integer | **absent** | object or `null` | — | 201, 1213, 1216, 1221, 1233 (no job), 1243 |
| **Q** printer query | **string** | integer | absent | **absent** | — | 1231, 1232 |
| **B** bare project | integer | integer | integer (0) | absent | — | 1206, 1214 |
| **J** project + data | integer | integer | integer | object | — | 6, 101–104, 1207, 1208, 1211, 1212, 1233 (with job) |
| **C** print control | integer | integer | integer | object or `null` | `ams_info` (object or null), `settings` (always null) | 1, 2, 3, 4 |
| **V** camera open | **string** | integer | absent | absent | `shengwang_rtc_support`: `true` (top level) | 1001 |

Load-bearing details:

- Shape P with `project_id` present (even 0): at least order 201 (axis move)
  is ignored by the printer.
- Shape P/Q/V with an **integer** `order_id`: ignored by the printer.
- Shape V without `shengwang_rtc_support`: the server returns
  `Video service upgraded. Update the slicer to enable.` and no credentials.
- `project_id` in shapes J/C is the latest job's `id` (E12) when the order is
  about a job (2, 3, 4, 6, 1233-with-job), otherwise `0`.

#### 5.2 Response

| Case | Meaning |
|---|---|
| `data` object with `msgid` (string, UUID-like) | accepted; `msgid` identifies the order (CMQTT replies may echo it). A `data` without `msgid` is logged as an empty reply and returns nothing. |
| `data` null, `msg` `No file found` | start-print file unknown (retry, §4.5) |
| `data` null, any other `msg` | failure, `msg` is the reason (e.g. `Print task does not exist`) |

#### 5.3 Order-id table

"Reply" = the CMQTT report kind/action that answers (CMQTT). "LAN form" = the
local message kind/action used instead when LAN is connected ("+ task id" =
the job id is added to the local `data` as a string `taskid`); "—" = cloud
only.

| Id | Name | Shape | `data` (see §5.4) | Reply | LAN form | 2.x trigger |
|---|---|---|---|---|---|---|
| 1 | START_PRINT | C | print request §5.4.1 | `print` / `start` … | — | print actions (§4.5) |
| 2 | PAUSE_PRINT | C, job id | `null` | `print` / `pause` | `print`/`pause` + task id | button `pause_print` |
| 3 | RESUME_PRINT | C, job id | `null` | `print` / `resume` | `print`/`resume` + task id | button `resume_print` |
| 4 | STOP_PRINT | C, job id | `null` | `print` / `stop` | `print`/`stop` + task id | button `cancel_print` |
| 6 | PRINT_SETTINGS | J, job id | `{settings:{…}}` §5.4.3 | `print` / `update` | `print`/`update` + task id | speed mode select/action; resin actions |
| 101 | LIST_UDISK_FILES | J, 0 | `{}` | `file` / `listUdisk` | — | button `request_file_list_udisk`; after USB delete |
| 102 | DELETE_UDISK_FILE | J, 0 | §5.4.10 | `file` / `deleteUdisk` | — | action `delete_file_udisk` |
| 103 | LIST_LOCAL_FILES | J, 0 | `{}` | `file` / `listLocal` | — | button `request_file_list_local`; after local delete; after CMQTT refresh |
| 104 | DELETE_LOCAL_FILE | J, 0 | §5.4.10 | `file` / `deleteLocal` | — | action `delete_file_local` |
| 201 | MOVE_AXLE | P | §5.4.6 | `axis` / `move` | `axis`/`move` | jog and home buttons |
| 1001 | CAMERA_OPEN | V | — | HTTP reply §5.4.14 | never diverted | cloud camera stream start |
| 1206 | MULTI_COLOR_BOX_GET_INFO | B | — | `multiColorBox` / `getInfo` | `multiColorBox`/`getInfo` | button `ace_refresh_spools` |
| 1207 | MULTI_COLOR_BOX_DRY | J, 0 | §5.4.8 | `multiColorBox` | `multiColorBox`/`setDry` | drying start/stop buttons |
| 1208 | FEED_FILAMENT | J, 0 | §5.4.9 | `multiColorBox` / `feedFilament` | `multiColorBox`/`feedFilament` | extrude/retract actions and buttons |
| 1211 | MULTI_COLOR_BOX_SET_SLOT | J, 0 | §5.4.11 | `multiColorBox` | `multiColorBox`/`setInfo` | `multi_color_box_set_slot_*` actions |
| 1212 | MULTI_COLOR_BOX_AUTO_FEED | J, 0 | §5.4.12 | `multiColorBox` / `setAutoFeed` | `multiColorBox`/`setAutoFeed` | run-out refill switches |
| 1213 | MOVE_AXLE_TURN_OFF | P | `null` | — | `axis`/`turnOff` | button *disengage motors* |
| 1214 | QUERY_AXIS_POSITION | B | — | `axis` / `query` | `axis`/`query` | button `request_axis_position` |
| 1216 | SET_TEMPERATURE | P | §5.4.4 | `tempature` (sic) | `tempature`/`set` | nozzle/bed numbers and actions (works idle) |
| 1221 | SET_FAN_SPEED | P | §5.4.5 | `fan` | `fan`/`setSpeed` | fan numbers and actions (works idle) |
| 1231 | QUERY_PERIPHERALS | Q | — | `peripherie` / `query` | `peripherie`/`query` | capability poll (BEH §3.14) |
| 1232 | GET_LIGHT_STATUS | Q | — | `light` / `query` | `light`/`query` | capability poll |
| 1233 | SET_LIGHT_STATUS | P (no job) or J (job id) | §5.4.7 | `light` / `control` | `light`/`control` | light entity |
| 1243 | SET_AI_SETTINGS | P | §5.4.13 | `aiSettings` | **— (cloud only)** | switch `ai_detection_enabled` |

Defined but never sent by the library or 2.x (names from the library's
enumeration; payloads unknown): 11 IGNORE, 12 DETECT, 44 STOP_PRINT_FORCE,
202 MOVE_AXLE_TO_COORDINATES, 301 START_EXPOSURE, 302 CANCEL_EXPOSURE,
501 START_RESIDUAL, 502 CANCEL_RESIDUAL, 601 SET_DEVICE_SELF_TEST,
602 GET_DEVICE_SELF_TEST, 701 SET_AUTO_OPERATION, 702 GET_AUTO_OPERATION,
801 RESET_RELEASE_FILM, 802 GET_RELEASE_FILM, 901 SET_PRINT_STATUS_FREE,
1002 CAMERA_CLOSE, 1209 FEED_FILAMENT_FINISH, 1210
MULTI_COLOR_BOX_REFRESH_SLOT, 1215 FILAMENT_CONTROL, 1224 FEED_RESIN,
1225 M7_AUTO_OPERATION, 1226 CYCLIC_CLEANING, 1227 SET_AUTO_FEED_INFO,
1228 GET_M7_AUTO_OPERATION, 1229 EXTFILBOX, 1230 GET_EXTFILBOX_INFO.

#### 5.4 Order payloads

##### 5.4.1 Order 1 — start print (`data`)

Common part (all three forms):

| Key | Type | Value |
|---|---|---|
| `filetype` | integer | **0** cloud file, **1** printer internal storage, **2** USB stick (2 is defined but never sent) |
| `file_key` | string | always `""` |
| `file_name` | string | always `""` |
| `task_settings` | object | `ai_detect` (integer, always 0), `camera_timelapse` (integer, always 0) |

Cloud form adds:

| Key | Type | Value |
|---|---|---|
| `file_id` | integer | cloud file id (§4.5) |
| `is_delete_file` | integer | 1 = delete the cloud file after printing (temp upload), else 0 |
| `project_type` | integer | always 1 |
| `template_id` | integer | always 0 |
| `matrix` | string | always `""` |
| `hollow_param`, `punching_param`, `slice_param`, `slice_size` | — | always `null` |

Local form adds `filename` (string, exact name as listed by order 103) and
`filepath` (string: `/` followed by the folder; 2.x always sends `/`).

Top-level for order 1: `project_id` 0, `settings` null, `ams_info` = the ACE
mapping or `null`.

`ams_info` when present:

| Key | Type | Meaning |
|---|---|---|
| `use_ams` | boolean | `true` whenever the mapping is non-empty |
| `ams_box_mapping` | array | one entry per colour of the file, §5.4.2 |

No leveling, flow-calibration, vibration-compensation or drying-first option
is sent by 2.x (the printer's `features` list suggests the slicer can send
such options; names not in these sources).

Constructed example — cloud file, one ACE, two colours:

```json
{
  "order_id": 1,
  "printer_id": "<printer_id: integer>",
  "project_id": 0,
  "data": {
    "filetype": 0,
    "file_key": "",
    "file_name": "",
    "task_settings": { "ai_detect": 0, "camera_timelapse": 0 },
    "file_id": "<cloud_file_id: integer>",
    "hollow_param": null,
    "is_delete_file": 0,
    "matrix": "",
    "project_type": 1,
    "punching_param": null,
    "slice_param": null,
    "slice_size": null,
    "template_id": 0
  },
  "ams_info": {
    "ams_box_mapping": [
      { "ams_color": [175, 175, 175], "ams_index": 0, "filament_used": 12.3,
        "material_type": "PETG", "paint_color": [175, 175, 175], "paint_index": 0 },
      { "ams_color": [33, 39, 33], "ams_index": 1, "filament_used": 4.1,
        "material_type": "PLA", "paint_color": [33, 39, 33], "paint_index": 1 }
    ],
    "use_ams": true
  },
  "settings": null
}
```

Constructed example — file already on the printer:

```json
{
  "order_id": 1,
  "printer_id": "<printer_id: integer>",
  "project_id": 0,
  "data": {
    "filetype": 1,
    "file_key": "",
    "file_name": "",
    "task_settings": { "ai_detect": 0, "camera_timelapse": 0 },
    "filename": "<file name as listed>",
    "filepath": "/"
  },
  "ams_info": null,
  "settings": null
}
```

##### 5.4.2 ACE slot mapping entry (`ams_box_mapping[]`)

Input: the user's slot list (0-based **global** slot numbers: box *n* owns
4n … 4n+3) and the file's colour list (`paint_infos`: `paint_index`,
`material_type`, `filament_used` g).

| Key | Type | Value |
|---|---|---|
| `ams_index` | integer | the global slot number chosen for this colour |
| `paint_index` | integer | the colour's `paint_index` from the file |
| `material_type` | string | the **file's** material for that colour |
| `filament_used` | number | the file's planned grams for that colour |
| `ams_color` | `[r,g,b]` | the **slot's** current colour (from the ACE record) |
| `paint_color` | `[r,g,b]` | also the **slot's** colour (identical to `ams_color`) |

Rules: the i-th slot in the user's list pairs with the i-th colour; the
highest slot must lie on a connected box (error otherwise); each box maps the
entries whose global slot minus 4 × (box `id`) is 0–3 and skips the rest; the
final list is sorted by `paint_index`.

##### 5.4.3 Order 6 — print settings (`data`)

`{"settings": {…}}` with **only** the keys being changed:

| Key | Type | Validated against (client side, before sending) |
|---|---|---|
| `print_speed_mode` | integer | must be one of the job's `print_speed_model_des` codes (E13); no list → refused |
| `target_nozzle_temp` | integer °C | within the job's `temp.limit.nozzle_temp_limit` (E13); no limits → refused |
| `target_hotbed_temp` | integer °C | within `hotbed_temp_limit` |
| `fan_speed_pct`, `aux_fan_speed_pct`, `box_fan_level` | integer | 0–100 |
| `bottom_layers` | integer | — (resin) |
| `bottom_time`, `off_time`, `on_time` | number, s | — (resin) |

`project_id` = latest job id. The server refuses with `Print task does not
exist` when no job is running. 2.x sends temperatures and fans through 1216
and 1221 instead (they work idle); order 6 is used for the speed mode and the
resin values only.

##### 5.4.4 Order 1216 — set temperature (`data`)

| Key | Type | Value |
|---|---|---|
| `type` | integer | 0 = nozzle only, 1 = bed only, 2 = both |
| `target_nozzle_temp` | integer °C | the nozzle target, **0 when not being set** |
| `target_hotbed_temp` | integer °C | the bed target, 0 when not being set |

Captured from the slicer's own traffic (recorded in the library):
`{type 0, bed 0, nozzle 230}`, `{type 1, bed 90, nozzle 0}`,
`{type 2, bed 60, nozzle 200}`. Works with or without a job. Nothing is sent
when neither target is given.

##### 5.4.5 Order 1221 — set fan speed (`data`)

Exactly **one** key per order, in this priority: `fan_speed_pct` (part fan,
%), else `aux_fan_speed_pct` (auxiliary fan, %), else `box_fan_level`
(BEH §9 V4). Integer values. Works idle.

##### 5.4.6 Order 201 — move / home axis (`data`, shape P)

| Key | Type | Values |
|---|---|---|
| `axis` | integer | 1 X, 2 Y, 3 Z, 4 X and Y (**not** Z) |
| `move_type` | integer | 0 negative, 1 positive, 2 home (distance ignored) |
| `distance` | integer mm | jog step (2.x offers 1, 15, 50); 0 for home |

The printer refuses jogs on an axis not homed since power-on (reply
`axis`/`move` state `failed`). *Home all* in 2.x = axis 4 home, wait until not
moving and not busy (polling E6 every 2 s, ≤ 45 s), then axis 3 home. 2.x
refuses moves while a job is in progress.

Order 1213 (disengage motors): shape P, `data` `null`; afterwards every axis
must be homed again. Refused by 2.x while printing.

##### 5.4.7 Order 1233 — set light (`data`)

| Key | Type | Value |
|---|---|---|
| `type` | integer | the light type the printer last reported (Kobra S1 = 2); **1** if never reported |
| `status` | integer | 1 on, 0 off |
| `brightness` | integer % | when on: requested value, default 100; when off: 0 |

Sent as shape J with the latest job's id when the printer has a latest job,
otherwise as shape P. (Both work; the job form is the one first proven
against the cloud.)

##### 5.4.8 Order 1207 — ACE drying (`data`)

`{"multi_color_box": [ box, … ]}`, each box:

| Key | Type | Value |
|---|---|---|
| `id` | integer | box id. If not given, the box's **position** in this list is used. |
| `drying_status.status` | integer | 1 start, 0 stop |
| `drying_status.target_temp` | integer °C | default 40 when not given |
| `drying_status.duration` | integer minutes | default 0 |
| `drying_status.remain_time` | — | always `null` |

On the wire every entry carries `id`. 2.x start: one entry, `status` 1, the
chosen duration and temperature, `id` 1 for the second ACE and 0 otherwise.
2.x stop on a named box: one entry with that `id`; stop with no box named:
one entry per connected ACE, ids 0…n−1 by position (BEH §7 G19).

##### 5.4.9 Order 1208 — feed / retract (`data`)

`{"multi_color_box": [ { "id": <box id>, "feed_status": { "slot_index": <int>, "type": <int> } } ]}`

| `type` | Meaning | `slot_index` |
|---|---|---|
| 1 | feed the slot through to the hotend | the slot (≥ 0; a negative slot is not sent) |
| 2 | retract whatever is loaded | forced to −1 |
| 3 | finish a feed | the slot |

Requires an ACE (nothing sent without one). The box defaults to 0.

##### 5.4.10 Orders 102 / 104 — delete a file on USB / internal storage (`data`)

| Key | Type | Value |
|---|---|---|
| `filename` | string | exact name as listed |
| `filetype` | integer | always −1 |
| `path` | string | always `/` |

Orders 101 / 103 (list) carry `data` `{}`. Listed entries arrive over CMQTT as
`filename`, `timestamp`, `size` (bytes), `is_dir` (BEH §2.5).

##### 5.4.11 Order 1211 — define an ACE slot (`data`)

`{"multi_color_box": [ { "id": <box id>, "slots": [ { "index": <0–3>, "color": [r,g,b], "type": "<material>" } ] } ]}`

Material strings sent by 2.x: `PLA`, `PETG`, `ABS`, `PACF`, `PC`, `ASA`,
`HIPS`, `PA`, `PLA SE`. `index` is the slot within the box.

##### 5.4.12 Order 1212 — run-out refill (`data`)

`{"multi_color_box": [ { "id": <box id>, "auto_feed": 0 | 1 } ]}`. 2.x sends
nothing when the cached state already matches.

##### 5.4.13 Order 1243 — AI detection settings (`data`)

`{"ai_settings": { … }}`:

| Key | Type | Value |
|---|---|---|
| `status` | integer | **3 = on**, **0 = off** |
| `type` | integer | as last reported by the printer, default 2 |
| `count` | integer | as last reported, default 60 |
| `sensitivity_level` | array of 2 integers | as last reported, default `[1, 1]` |
| `notice_type` | array of 2 integers | as last reported, default `[0, 1]` |

The current settings are **not** readable over HTTP; they come only from the
printer's `aiSettings` reports (CMQTT, LAN). Cloud only (no LAN form).

##### 5.4.14 Order 1001 — open the cloud camera

Request (shape V, constructed):

```json
{ "order_id": "1001", "printer_id": "<printer_id: integer>", "shengwang_rtc_support": true }
```

Response `data` (credentials are in the **HTTP reply**):

| Field | Type | Meaning |
|---|---|---|
| `msgid` | string | order id |
| `shengwang.appid` | string | Agora app id (**sensitive**) |
| `shengwang.channel` | string | channel name |
| `shengwang.rtc_token` | string | join token (**secret**, single use) |
| `shengwang.client_uid` | integer | this viewer's uid, fresh on every call |
| `shengwang.uid` | integer | printer's uid (fallback when `shengwang_device` is absent) |
| `shengwang.encryption_mode` | string | e.g. `AES_256_GCM2`, or `none` |
| `shengwang.encryption_key`, `shengwang.encryption_kdf_salt` | string | media encryption (**secret**) |
| `shengwang.event_id` | string | — |
| `shengwang_device.uid` | integer | printer's uid; this block is absent for some accounts |

Missing `shengwang` block with `msg` `Operation successful` = either the
printer has no camera **or** another Anycubic session (slicer, phone app)
owns the camera. 2.x retries **once** after forcing a fresh login (drops the
cached user token, E1 + E3), then gives up. That retry is only possible with
a slicer token that has a cached user token; with any other token kind there
is nothing to refresh and 2.x gives up at once. Credentials are short-lived;
ask per viewing session, never cache.

---

### 6. Firmware updates

#### 6.1 E24 — Printer firmware (`GET /work/printer/update_version`)

| Aspect | Value |
|---|---|
| Query | `id` (printer id, string), `target_version` (string) — **2.x puts the currently installed version here** (`version.firmware_version`), not the target |
| Precondition (client) | `version.need_update` is 1; otherwise nothing is sent |
| Response `data` | `update_status` — **1 = update started** |
| After | progress arrives over CMQTT `ota` reports (CMQTT; BEH §2.17) |
| When (2.x) | update entity *install* on `fw_version`, after waking CMQTT; then a forced refresh |

#### 6.2 E25 — ACE firmware (`POST /v2/printer/update_multi_color_box_version`)

| Aspect | Value |
|---|---|
| Body | `id` (printer id, **integer**), `box_id` (integer: 0 first ACE, 1 second — the index into `multi_color_box_version`) |
| Precondition (client) | printer has an ACE; that entry's `need_update` is 1 |
| Response `data` | `target_version` (string) — success when it equals that entry's `target_version` from E6 |
| When (2.x) | update entity *install* on `multi_color_box_fw_version` (box 0) or `secondary_multi_color_box_fw_version` (box 1) |

The library can also loop over every box; 2.x does not use that.

---

### 7. AI detection settings

There is no HTTP endpoint that reads or writes AI detection directly. Writing
is order 1243 through E23 (§5.4.13); reading comes from printer reports. The
start-print `task_settings.ai_detect` flag exists but is always sent as 0.
Capability is function id 36 in `type_function_ids` (§2.3.9).

---

### 8. When 2.x calls what

#### 8.1 Setup and flows

| Moment | Calls, in order |
|---|---|
| Config flow, credentials step | E1 (slicer tokens) + E3 |
| Config flow, printer step | E4 (picker; parse faults tolerated) → on submit E6 per selected printer |
| Options flow | E1/E3, then E4 (does any printer have function 2006? → offer drying presets) |
| Entry setup (cloud, LAN off) | E1 (if needed) + E3 → E6 for the **first** printer (reachability) → E6 per printer (build objects). No job calls yet. Setup is retried up to **3** times, **10 s** apart, on a "server maintenance/parse" error. |
| Entry setup with LAN Mode on | E3 only; E6 attempted per printer and allowed to fail (the LAN builds the printer) |
| LAN-only entry | no HTTP at all |
| Diagnostics download | E3, E4, E12 (raw) → E13 for the account's newest job → E6 per printer (raw) |

#### 8.2 The cloud poll

The coordinator ticks every **15 s**; the cloud poll runs at most every
**60 s** and is skipped while LAN is connected (BEH §5.1). One poll:

1. E3 (token check; E1 first when a slicer token has no cached user token).
2. For each printer: E6, then E12, then E13 for that printer's latest job.
3. CMQTT management, new-printer pickup (E6 for any selected printer not yet
   loaded), capability poll (orders 1231/1232 via E23, only while CMQTT is up,
   online printers, ≤ 3 per printer), filament ledger.

Per poll with *n* printers: 1 + 3n requests (E12 is repeated per printer,
each asking for up to 2000 jobs).

Failure handling: any exception fails the poll; after **3** consecutive
failed polls the next is deferred by **240 s** (about 5 minutes including the
normal 60 s).

#### 8.3 On-demand calls

| Trigger | Calls |
|---|---|
| Any button/number/switch/select/most actions | the order via E23 (unless LAN-diverted), then an immediate full poll (§8.2); the next poll follows ~10–15 s later |
| CMQTT reports a print started | full poll 5 s later |
| Button `request_file_list_cloud` | wakes CMQTT, then E17 (page 1, limit 10) |
| Action `delete_file_cloud` | E18; on success E17 5 s later |
| Actions `print_and_upload_*` | §4.4 and §4.5 (E19, E20, X1, E21, E22, E19, E17, E16, E23) |
| Update entity install | wakes CMQTT, then E24 or E25, then a full poll |
| Cloud camera stream start | E23 order 1001 (and E1/E3 on the retry) |
| *Home all* | order 201 home XY → E6 (no job calls) every 2 s until idle, ≤ 45 s → order 201 home Z |

#### 8.4 Retries that exist

| What | Rule |
|---|---|
| Access-token login | 2 attempts, 2 s apart; then retried as a web token |
| Credentials check | once more after dropping a cached slicer user token |
| Setup | 3 retries, 10 s apart, on parse/maintenance errors |
| Start print | 3 attempts, 3 s apart, on `No file found` |
| Camera open | 1 retry behind a fresh login |
| Home-all wait | E6 every 2 s, ≤ 45 s |
| G-code file read (action) | 3 attempts, 1 s apart (local file, not HTTP) |

---

### 9. Pagination and limits

| Constant / rule | Value | Where |
|---|---|---|
| the job-list limit | 2000 | `limit` on E12; always page 1 — no paging loop |
| the image-search limit | 200 | records scanned past the latest job to borrow an image (§3.1.4) |
| Cloud file list | page 1, limit 10 (library default) | E17 — only the 10 newest files are ever seen |
| ACE slots per box | 4 | slot arithmetic (§5.4.2) |
| ACE boxes addressed by 2.x | 2 (more are counted, not addressable) | E6 / BEH §1.7 |
| Slow-call warning | > 20 s, at most every 10 min | all calls |
| Cloud poll interval | 60 s (tick 15 s) | §8.2 |
| Failure back-off | after 3 failures, +240 s | §8.2 |

---

### 10. Known server quirks

| # | Quirk | Evidence |
|---|---|---|
| Q1 | `code` is never checked; decisions hang on `data` null-ness and `msg` text. | §0.3 |
| Q2 | Orders in the wrong shape are **accepted** (`Operation successful`) but ignored by the printer: `order_id` must be a string for printer-level, query and camera orders and an integer for project/control orders; printer-level orders must omit `project_id`. | §5.1 |
| Q3 | Order 6 (print settings) is refused idle with `Print task does not exist`; temperatures and fans have idle-capable orders 1216 and 1221. | §5.4.3–5.4.5 |
| Q4 | `multi_color_box` in E6 is an object for one ACE and an array for several. | §2.3.10, fixture |
| Q5 | `external_shelves` is sent all-null when no holder exists (firmware 2.0.1.9). | §2.3.8, hass-anycubic #28 |
| Q6 | Job `status` can be `null` (seen on the China region); `0` is a distinct real value. | §3.1, library test |
| Q7 | Job `settings`, `slice_param`, `slice_result` may be JSON-encoded strings or objects. | §3.1 |
| Q8 | Lifetime text fields: `material_used` like `18.17kg`; `print_totaltime` / `total_time` either minutes as a number-in-text or `<h>hour<m>min`. | §2.3.2 |
| Q9 | E4 puts `material_used`, `print_totaltime`, `material_type`, `description`, `machine_mac` at top level; E6 puts them under `base`. | §2.1, §2.3.2 |
| Q10 | `version.target_version` equals the installed version when no update is available. | fixture |
| Q11 | Case-sensitive look-alike paths: `/v2/Printer/status`, `/work/printer/Info` vs `/v2/printer/info`. | §0.1 |
| Q12 | A printer in LAN Mode is reported deleted (`code` 1007) and vanishes from E4; re-adding in the app restores the same cloud id. | 2.x README, §2.3 |
| Q13 | Camera open answers `Operation successful` without credentials when another session owns the camera; a fresh login reclaims it. | §5.4.14 |
| Q14 | A fresh upload's `gcode_id` can still be null on the first E17 read (the cloud parses asynchronously); 2.x then fails *save in cloud*. | §4.5 |
| Q15 | The storage PUT signals success only by an empty body; the status code is not a reliable signal to the library. | §4.4 |
| Q16 | E12 (up to 2000 records) is called once per printer per poll — heavy; the list is account-wide. | §8.2 |
| Q17 | Null inside otherwise-present objects (`machine_data`, `parameter`, `version.need_update`, `version.firmware_version`) fails the 2.x parse and, on refresh, the whole poll. Nulls in tools, ACE telemetry and external holder are tolerated. | §2.3 |
| Q18 | Firmware install query names its parameter `target_version` but 2.x sends the **installed** version in it, and this works. | §6.1 |
| Q19 | Delete success is signalled by `data` equal to the empty string. | §4.2 |
| Q20 | `request error` in `msg` is used by the server for both maintenance (user info) and apparent throttling (printer detail). | §0.3 |

---

### 11. Open points

| # | Question |
|---|---|
| O1 | The success value of the top-level `code` for HTTP is never asserted in the sources. Capture one successful reply of each endpoint to fix it (and confirm 1007's text). |
| O2 | ACE box numbering: the captured E6 shows the single ACE with `multi_color_box.id` **1** while `multi_color_box_version[0].box_id` is **0**, and LAN reports show `id` 0. The slot arithmetic (§5.4.2) and ACE orders use `id`; with `id` 1 every slot 0–3 would be skipped. Confirm whether the fixture value is genuine and which numbering the orders expect. |
| O3 | Unit of `estimate` on job records (E12) and on E16 (seconds on E17 file records per the library; unconfirmed for the other two). Unit of `version.time_cost`. |
| O4 | Content-Type for the storage PUT: the library sends none of its own, so its HTTP client's default for raw bytes (believed `application/octet-stream`) is what the pre-signed URL has been accepting. Confirm the header the pre-signed URL requires. |
| O5 | Meaning of `machine_type` 0 and `printable` in E17, of printer-record `type`, `status`, `ready_status`, `available`, `color`, and of function ids 43–48. |
| O6 | Additional start-print `task_settings` keys the slicer sends (auto-leveling, flow calibration, vibration compensation, drying first, AI detect on, timelapse on) — names and values are not in these sources. |
| O7 | E2, E7, E9, E10, E14, E15: parameters and responses unknown (never exercised). E14 in particular could replace the 2000-record E12 scan for job history. |
| O8 | Real types of `tools[].id` and `external_shelves.id` (scrubbed in the only capture). |
| O9 | Whether the cloud rate-limits E12 at 1 + 3n calls per minute; 2.x's `request error` → "rate limited?" mapping suggests throttling exists. |
| O10 | The unlock-after-failure path (`is_delete_cos` 1) is never sent by 2.x (§4.4 quirk); behaviour of a lock that is never released is unknown. |
| O11 | China region: base/auth domains came from one user report; the API path (`p/p/workbench/api`) and the image URL base are assumed shared. Unverified. |
| O12 | Whether the `features` and `temp_limit` / `free_temp_limit` blocks of E6 (ignored by 2.x) should feed 3.0 (e.g. preheat limits, timelapse capability). Decision for the implementation team. |

---

## Part C — Cloud MQTT (CMQTT)

Specification-team facts for the clean-room rewrite. Written from reading the
2.x integration (`anycubic_cloud` 2.9.x), the library it pins
(`anycubic-cloud-api` 0.4.32), their tests, the MQTT client library 2.x runs on
(paho-mqtt 2.1.0, for its defaults), and the maintainer's research notes
(broker captures, slicer traffic captures). No code is reproduced; behaviour is
described in prose, tables and short expressions.

**Cross-references (do not duplicate).**

- **LAN §x** = `anycubic-lan/docs/PROTOCOL.md`. The printer uses largely the
  same report bodies on its own LAN broker; where a cloud body is the same, this
  document says "same body as LAN §x" and lists only the differences.
  **LAN Qn** = `anycubic-lan/docs/QUESTIONS.md`.
- **BEH §x** = `hass-anycubic-next/docs/BEHAVIOUR.md`, which calls this
  transport **CMQTT** and already specifies entity-level presentation, the
  connect-mode table (BEH §5.2), capability polling (BEH §3.14) and error-code
  presentation (BEH §1.4). This document adds the wire level and the exact 2.x
  timing underneath.
- **D §x** = sibling `D-camera-print-files-ota.md` (HTTP order envelope in
  D §0.2, camera, printing, file lists, firmware). The account login, token
  exchange and HTTP request signing are in the sibling transport/auth spec.

**Placeholders used in examples:** `<USER_ID>` (decimal account id),
`<USER_ID_MD5>`, `<EMAIL>`, `<MOBILE>`, `<CLIENT_ID>` (32 lowercase hex),
`<PRINTER_ID>`, `<PRINTER_KEY>`, `<MACHINE_TYPE>` (model id such as `20025`),
`<MSGID>`, `<TASK_ID>`, `<LOCALTASK_UUID>`, `<USER_TOKEN>`, `<PASSWORD>`,
`<SIG>`. No secret value appears anywhere in this document.

**Evidence labels.** *Code* = what 2.x / the library does. *Captured* = seen on
real traffic or hardware (from notes or tests quoting real payloads).
*Constructed* = an example built from the fields the parser reads; not a
capture. *Inferred* = deduced, not observed.

---

### 1. Connection

#### 1.1 Broker per region

| Region (config value) | Broker host | Port | Hostname verification | Status of the facts |
|---|---|---|---|---|
| `international` (default; also any absent/unknown stored region) | `mqtt-universe.anycubic.com` | 8883 | **on** | Maintainer-verified; the Anycubic slicer connects to the same host:port (captured). |
| `china` | `mqtt.anycubicloud.com` | 8883 | **off** (chain verification still on) | Host reported by one user (hass-anycubic #13), not maintainer-verified. Port **assumed** (not reported). Field-observed TLS error with hostname checking on: "certificate is not valid for 'mqtt.anycubicloud.com'". |

The region is chosen once by the user; nothing probes it. The endpoint set is
fixed for the life of an API object (client id, cookie jar and signed headers
are bound to it).

#### 1.2 Transport and TLS

| Item | Value |
|---|---|
| Transport | TCP + TLS (MQTT over TLS), no WebSocket |
| TLS versions | minimum **TLS 1.2**; no maximum is set (1.3 allowed if the broker offers it) |
| Server name indication | the broker host name (the MQTT client always sends SNI = host) |
| Trust anchor | **only** the pinned Anycubic CA certificate (resource `secrets.mqtt_ca_pem`). No system/public trust store is loaded. The broker certificate is issued by this private CA. |
| Chain verification | always **required** (both regions) |
| Hostname check | per region (§1.1). Notes record that the international broker certificate carries **no subjectAltName**, so the host matches only through the certificate subject's common name (OpenSSL's CN fallback). A TLS stack that refuses CN fallback will fail the international handshake. |
| Strict X.509 mode | must be **off**: the Anycubic root CA omits the `keyUsage` extension, which strict verification (e.g. Python ≥ 3.13's default `VERIFY_X509_STRICT`) rejects as a trust anchor. Only that flag is cleared. |
| Security level | OpenSSL security level **0** with the default cipher list (`DEFAULT:@SECLEVEL=0`). Needed because the client certificate is **SHA-1 signed**, which OpenSSL 3 refuses to load at the default level. The relaxation is for the certificate signature, not for ciphers. |
| Client authentication | **mutual TLS**: the client presents `secrets.mqtt_client_cert_pem` with its private key `secrets.mqtt_client_key_pem` (no key passphrase). |

#### 1.3 The TLS material (by `CloudSecrets` field)

| Field | What it is | Used for |
|---|---|---|
| `secrets.mqtt_ca_pem` | Anycubic's private root CA certificate (PEM, RSA key) | (a) the only trust anchor for the broker; (b) **its RSA public key** encrypts the MQTT password in Slicer mode (§1.5) |
| `secrets.mqtt_client_cert_pem` | client certificate, SHA-1 signed; notes record subject `CN=AnycubicSlicer` | mutual TLS |
| `secrets.mqtt_client_key_pem` | the matching RSA private key | mutual TLS |

This material is Anycubic's. The library receives it from its caller and never
ships it (CLEAN-ROOM.md, INTEGRATION-SPEC §2).
If the CA file is missing, 2.x refuses to start MQTT with "no certificate
found" and the Slicer-mode password cannot be computed.

#### 1.4 Account identity and client id

Inputs, all from the HTTP user-info call (`GET /user/profile/userInfo`, reply
`data`; see the sibling auth spec):

| Name | Source field | Notes |
|---|---|---|
| `user_id` | `data.id` | integer; if absent/non-numeric the login is treated as failed |
| `email` | `data.user_email` | an empty string is treated as absent |
| `mobile` | `data.mobile` | empty treated as absent; China accounts have a mobile and `user_email: ""` |

Derived:

- `identity_string` = `email` if present, else `mobile`. If neither exists, 2.x
  raises "Unable to build mqtt_client_id: the account has neither an email nor
  a mobile number" **before opening any socket**.
- `identifier` (for the username, §1.5) = `email`, else `mobile`, else
  `str(user_id)` (the last fallback is unreachable in practice because the
  client id needs email or mobile first).
- **Client id**:
  - Slicer mode: `client_id = md5_hex(identity_string + "pcf")`
  - Android mode (and Web, which never connects): `client_id = md5_hex(identity_string)`

  `md5_hex` = MD5 over the UTF-8 bytes, rendered as 32 **lowercase** hex digits.
  The literal suffix `pcf` is what the Anycubic slicer appends before hashing
  (the same client id as the user's own slicer — see §6.15).
- `user_id_md5` = `md5_hex(decimal string of user_id)` (used in user topics, §2.2).

China: the client id is expected to be `md5_hex(mobile + "pcf")` in Slicer mode;
notes flag this as "awaiting reporter confirmation".

#### 1.5 Username and password per auth mode

The auth modes are those of the sibling auth spec: 1 Web, 2 Android, 3 Slicer.

| Auth mode | CMQTT allowed in 2.x | `role` literal | Password (`mqtt_token`) |
|---|---|---|---|
| 1 Web | **no** (`supports_mqtt_login` false; the config flow labels it "Web (No MQTT)") | — | — |
| 2 Android | yes | `app` | `bcrypt( md5_hex(user_token) )` |
| 3 Slicer | yes | `pcf` | `base64( RSA_PKCS1v1.5_encrypt( CA_public_key, UTF-8(user_token) ) )` |

`user_token` is the account user token used for HTTP calls (the `XX-Token`
value): in Slicer mode it is the token obtained by exchanging the pasted slicer
access token (China: the pasted token used directly, no exchange); in Android
mode the pasted token. Details in the sibling auth spec.

Password detail:

- **Slicer**: encrypt the whole token in one RSA block with PKCS#1 v1.5 padding
  using the public key of `secrets.mqtt_ca_pem`; encode the
  ciphertext with standard Base64 (with `=` padding); the password is that ASCII
  text. No chunking is done (the token must fit one block).
- **Android**: take `md5_hex(user_token)` (32 lowercase hex chars) and hash that
  text with bcrypt using a **fresh random salt** at the bcrypt library's
  defaults (cost 12, `$2b$` variant); the password is the resulting 60-character
  bcrypt string.
- Both are **randomised**: every computation yields a different password (RSA
  padding / bcrypt salt), and therefore a different username signature.

Username:

```text
username = "user|" + role + "|" + identifier + "|" + sig
sig      = md5_hex( client_id + password + client_id )
```

(the "username-token sandwich": client id, then the password text exactly as
sent, then the client id again; hashed as UTF-8, lowercase hex).

Example (placeholders only):

```json
{
  "client_id": "<CLIENT_ID>",
  "username": "user|pcf|<EMAIL>|<SIG>",
  "password": "<PASSWORD — base64 of RSA ciphertext in Slicer mode>"
}
```

#### 1.6 MQTT session parameters

| Parameter | Value in 2.x | Source |
|---|---|---|
| Protocol | MQTT **3.1.1** | client default |
| Clean session | **true** | code |
| Keep-alive | **1200 s** (20 min) | code constant 2.x's keep-alive setting |
| Will message | none | code |
| Subscribe QoS | **0** for every subscription | client default |
| Publish | nothing is published by 2.x (§5) | code |
| Payload encoding | UTF-8 JSON object | code |
| Connect call | synchronous TCP + TLS handshake + CONNECT, then a blocking network loop on a worker thread | code |
| Reconnect delay | first wait **5 s**, doubled after each consecutive failure, capped at **120 s**; reset to 5 s after a successful CONNACK | code sets min 5; cap is the client default |
| Credentials | computed at start and **recomputed after every unexpected disconnect** before the automatic reconnect (tokens may have been refreshed by HTTP; and the values are randomised anyway) | code |

Notes (captured behaviour, cause not proven): hand-derived Slicer credentials
"go stale fast" — a capture had to connect within seconds of deriving them, and
every CONNACK `rc=5` (not authorised) seen during research was stale/expired
credentials, never contention.

#### 1.7 Differences from the LAN connection (LAN §3–§4)

| Aspect | Cloud (this doc) | LAN |
|---|---|---|
| Broker | fixed cloud host per region, port 8883 | the printer, port from the handshake (9883 observed) |
| Certificate check | pinned private CA, chain verified; hostname per region | none (self-signed) |
| Client certificate | required (mutual TLS) | none |
| Credentials | derived locally from the account token (§1.5) | issued by the printer's signed handshake |
| Client id | `md5` of account identity; **shared with the user's own slicer / phone app** | any unique string |
| Keep-alive | 1200 s | 60 s |
| Who asks for state | the cloud **pushes** unprompted; replies to HTTP orders also arrive here | the client must publish queries periodically |
| Publishing | none | queries and commands on `.../web/printer/...` |

---

### 2. Topics

#### 2.1 Prefix and segment numbering

Every topic starts `anycubic/anycubicCloud/v1`. Segments are numbered from 0
after splitting on `/`:

| Index | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|---|
| Meaning | `anycubic` | `anycubicCloud` | `v1` | origin: `printer`, `server`, `web`, `pc`, `app` … | audience: `public`, `app`, `printer` | `<MACHINE_TYPE>` (printer topics) or `<USER_ID>` (user topics) | `<PRINTER_KEY>` or `<USER_ID_MD5>` | report kind, or `response` | usually `report` | ACE OTA: box index |

`<MACHINE_TYPE>` and `<PRINTER_KEY>` are the HTTP printer record's
`machine_type` (integer model id, e.g. 20025 Kobra S1, 20030 Kobra X) and `key`
(string). The printer key is treated as sensitive: every 2.x log line that
prints a printer topic replaces segment 6 with `**REDACTED**`.

#### 2.2 Subscriptions 2.x makes

| # | Topic filter | Scope | When subscribed |
|---|---|---|---|
| U1 | `anycubic/anycubicCloud/v1/server/app/<USER_ID>/<USER_ID_MD5>/slice/report` | per user | on every successful CONNACK |
| U2 | `anycubic/anycubicCloud/v1/server/app/<USER_ID>/<USER_ID_MD5>/fdmslice/report` | per user | on every successful CONNACK |
| P1 | `anycubic/anycubicCloud/v1/printer/app/<MACHINE_TYPE>/<PRINTER_KEY>/#` | per printer | on every successful CONNACK, for each printer in the subscription set |
| P2 | `anycubic/anycubicCloud/v1/+/public/<MACHINE_TYPE>/<PRINTER_KEY>/#` | per printer | same as P1 |

- Order: U1, U2, then P1 and P2 for each printer; one SUBSCRIBE per filter,
  QoS 0. The first SUBACK is what 2.x treats as "connected" (§6.9).
- The subscription set is filled with **every loaded printer** just before a
  connection is started (§6.5). A printer loaded later while the link is up is
  **not** subscribed until the next (re)connect (see Open points).
- On a deliberate stop 2.x unsubscribes P1 and P2 for every printer and empties
  the set; U1/U2 are not unsubscribed (the clean-session disconnect drops them).
- Notes call these three filter families "the permitted topics" (the broker
  grants them to this identity); the 60-second capture in §4.19 covered all of
  them. Whether anything else is grantable is untested apart from §2.3.

#### 2.3 Topics that exist but 2.x does not use

| Topic | Fact |
|---|---|
| `anycubic/anycubicCloud/v1/pc/printer/<MACHINE_TYPE>/<PRINTER_KEY>/#` | The slicer's own topic family. Subscribing is **refused by ACL** (SUBACK return code 128, captured). Present in the library only as a commented-out subscription. |
| `anycubic/anycubicCloud/v1/printer/public/<MACHINE_TYPE>/<PRINTER_KEY>/<endpoint>` | A publish-topic builder exists in the library but is **never called**. |
| `anycubic/anycubicCloud/v1/app/` | Prefix constant present, never used. |
| `anycubic/anycubicCloud/v1/web/printer/<MACHINE_TYPE>/<PRINTER_KEY>/video/report` | The slicer's web view publishes camera telemetry here after joining a cloud video session (captured from the slicer's bundle). Not used by 2.x. |

#### 2.4 Report topic shapes (inferred)

The library's LAN client states that LAN report topics have the **same shape**
as cloud ones, with model id and device id in place of machine type and printer
key, and 2.x feeds both to the same parser. So cloud reports arrive on
`anycubic/anycubicCloud/v1/printer/public/<MACHINE_TYPE>/<PRINTER_KEY>/<kind>/report`
(and deeper), matched by P2; replies/acknowledgements arrive on
`anycubic/anycubicCloud/v1/printer/app/<MACHINE_TYPE>/<PRINTER_KEY>/response`
(index 7 = `response`), matched by P1. The exact ACE firmware topic is not
known; 2.x only needs "contains `multiColorBox`" and index 9 (§3.5).

#### 2.5 Routing rule for an incoming message (2.x)

1. Decode the payload as UTF-8 JSON. Failure → ERROR log "Message decode
   error" with topic and raw payload; message dropped.
2. If segment 3 is `server` → **user message**: logged at debug, nothing else
   (no state change, no refresh).
3. Otherwise it is a printer message; printer key = segment 6.
4. If segment 7 is `response` **and** the payload object has exactly one key →
   dropped silently (bare acknowledgements such as `{"msgid": "..."}`).
5. If the key is not in the subscription set → dropped silently.
6. Apply the message to that printer (§3, §4). Whatever happens inside (applied,
   unknown kind, unhandled leftovers, exception), the "data updated" callback
   then fires (§6.12), and the "print started" callback fires if the printer
   went from free to busy during this message.

---

### 3. Message envelope

#### 3.1 Top-level fields

Same envelope as LAN §5. Every field 2.x reads or consumes:

| Field | Type | Meaning | Use in 2.x |
|---|---|---|---|
| `type` | string | report kind (§4). **Required**: a printer-topic payload without it raises (ERROR log, nothing applied). | dispatch |
| `action` | string | sub-kind; for replies it echoes the request's verb (`query`, `getInfo`, `setDry` …); for pushes `auto`, `report`, `workReport`, `onlineReport`, `autoUpdate…` | dispatch; **required** like `type` |
| `state` | string or absent | result/progress word: `done`, `success`, `failed`, or a phase (`printing`, `busy`, `online`, …) | dispatch |
| `code` | integer | result code (§3.2) | recorded before dispatch |
| `msg` | string | text accompanying `code`; for `print` `failed` it is the failure reason | recorded with a fault code; used as the job's failure reason |
| `msgid` | string | message id (§3.3) | consumed, ignored |
| `timestamp` | number | not interpreted by 2.x; on LAN it is an uptime-like counter; cloud semantics unknown | consumed, ignored |
| `data` | object, list or `null` | the body | per kind |

Inside `data`, 2.x additionally consumes and ignores `taskid` and `localtask`
on every kind (they matter only to `print`).

Differences from LAN: none known in the envelope itself. Cloud pushes use
`action: auto` where LAN replies use `query` for `tempature` and `fan`
(same bodies).

#### 3.2 `code` semantics

- `200` = "message processed", nothing wrong. `0` is also treated as OK.
- Any other **integer** (booleans and non-integers ignored) is a printer fault
  code from Anycubic's published list (wiki.anycubic.com/en/error-codes;
  2.x carries ~95 codes in 10000–11871). Codes not on the list exist (11858 =
  Kobra X ACE slot ran dry, #21) and are reported as `Unknown error code <n>`.
- 2.x reads `code` and `msg` from **every** printer message **before**
  dispatching it, so the fault is kept even when the message kind is unknown or
  the handler fails. The last fault code and message are kept in memory until
  another fault replaces them; an OK code does **not** clear them (BEH §1.4,
  gap G7).
- A failed command reply carries `state: failed` and a non-200 `code` (LAN Q2).

History worth knowing: an earlier maintainer note claimed the envelope code is
"always 200"; that was wrong — 200 is only what it reads while nothing is wrong.

#### 3.3 Request/reply correlation

- 2.x never publishes on the cloud broker. A command is an HTTP order
  (D §0.2); the HTTP reply carries `data.msgid`. The library's documentation
  states that the printer echoes this `msgid` in the report that follows.
- 2.x does **not** correlate by `msgid`. A reply is recognised only by its
  `type`/`action`/`state`, and applied to whatever printer the topic names.
- Job-scoped reports (`print`) are matched to the known job by `data.taskid`
  (§4.4).

#### 3.4 Parsing contract in 2.x (what a reimplementation must match or improve)

The library treats each payload as "consumable": every key the handler reads is
marked used. After dispatch:

| Outcome | 2.x log level | State already applied? |
|---|---|---|
| All keys used | none | yes |
| Keys left over | WARNING "Message unhandled data" with type/action/state and the leftovers | **yes** — leftovers are only reported |
| Kind/action/state pair not handled | DEBUG "Message not understood" with payload | nothing for that message (the fault code, §3.2, is still kept) |
| Handler raised (e.g. a required field null) | ERROR with traceback | partially, up to the failure |

Requirement from BEH §6 (B3, B36): one bad field must never discard the rest;
unknown kinds are logged quietly.

#### 3.5 Topic-derived facts used by the parser

- An `ota` message whose **topic contains `multiColorBox`** is the ACE's
  firmware report; the box index is segment 9 if it is all digits, else 0.
- No other handler looks at the topic.

---

### 4. Message catalogue

#### 4.0 Master table

Legend: **Handled** = changes state in 2.x. **Cloud-only** = no LAN
equivalent. Push = unprompted.

| `type` | `action` | `state` | Trigger | Handled | LAN |
|---|---|---|---|---|---|
| `lastWill` | `onlineReport` | `online` / `offline` | push (broker/printer presence) | yes | cloud-only |
| `status` | `workReport` | `free` / `busy` | push | yes | cloud (LAN client lists `workReport` among local actions too) |
| `user` | `bindQuery` / `unbind` | `done` | push on (un)binding | yes (flag only) | cloud-only |
| `print` | `start` | `downloading`, `checking`, `preheating`, `printing`, `finished`, `stopping`, `stoped`, `failed`, `updated` | push during a job; reply to orders 1/4/6 | yes | LAN Q2 |
| `print` | `pause` | `pausing`, `paused` | reply to order 2 | yes | LAN Q2 |
| `print` | `resume` | `resuming`, `resumed` | reply to order 3 | yes | LAN Q2 |
| `print` | `stop` | `stopping`, `stoped`, `failed` | reply to order 4 | yes | LAN Q2 |
| `print` | `stop` | `stopped` (double p) | reply to order 4 | **no** (not understood) | LAN Q2 |
| `print` | `update` | `updated` | push during a job; reply to order 6 | yes | LAN §7.2 note |
| `print` | `getSliceParam` | `done` | reply | yes | — |
| `tempature` (sic) | `auto` (cloud), `query` (LAN) | `done` | push | yes | same body as LAN §6.3 |
| `fan` | `auto` / `query` | `done` | push | yes | same body as LAN §6.4 |
| `light` | any (`query`, `control`, …) | `done` | reply to 1232 / 1233 | yes | same body as LAN §6.5 |
| `peripherie` (sic) | `query` | `done` | reply to 1231 | yes | same body as LAN §6.9 |
| `axis` | `query` | `done` | reply to 1214 | yes | same body as LAN §6.6 |
| `axis` | `move` | `doing` / `done` / `failed` (any state accepted) | reply/progress for order 201 | yes | same body as LAN §6.6 |
| `multiColorBox` | `getInfo` | `success` | reply to 1206 | yes | same body as LAN §6.7 |
| `multiColorBox` | `setInfo`, `refresh` | `success` | reply to 1211; push | yes | LAN Q1 |
| `multiColorBox` | `autoUpdateInfo` | `done` | push (loaded slot changed) | yes | LAN Q1 (shape differs, §4.10) |
| `multiColorBox` | `autoUpdateDryStatus`, `setDry` | `success` | push while drying; reply to 1207 | yes | LAN Q1 |
| `multiColorBox` | `feedFilament` | `done` | reply/progress for 1208 | yes | LAN Q1 |
| `multiColorBox` | `setAutoFeed` | `done` | reply to 1212 | yes | LAN Q1 |
| `extfilbox` | `reportInfo` | `success` | push | yes (if the HTTP record has a holder) | same body as LAN §7.2 |
| `file` | `listLocal` / `listUdisk` | `done` | reply to 103 / 101 | yes | cloud-only |
| `file` | `deleteLocal` / `deleteUdisk` | `success` | reply to 104 / 102 | acknowledged, no state | cloud-only |
| `file` | `cloudRecommendList` | `done` | push | ignored wholesale | cloud-only |
| `ota` (printer) | `reportVersion` | `done` | push after update/boot | yes | cloud-only |
| `ota` (printer) | `update` | `start`, `downloading`, `updating` | push during a firmware update | yes | cloud-only |
| `ota` (ACE topic) | `reportVersion` | `done` | push | yes | cloud-only |
| `ota` (ACE topic) | `update` | `start`, `downloading`, `updating`, `update-success`, `updateSuccessProcessed` | push | yes (success states ignored) | cloud-only |
| `aiSettings` | any | any | push "when the printer volunteers it" | yes | same body as LAN §6.8 |
| `event`, `printerevent`, `printer_event` | any | any | push on a printer fault | yes (code only) | — |
| `info` | any | any | **not seen on the cloud** (LAN only) | yes if it ever arrives | LAN §6.1 |
| `video` | any | any | — | no (not understood) | LAN §7.2 (`startCapture`) |
| user topics U1/U2 | — | — | cloud slicing progress for the account | logged only | cloud-only |

#### 4.1 `lastWill` — online/offline

Cloud-only. `action` `onlineReport`:

| `state` | Effect (device status) |
|---|---|
| `online` | 1 (online) |
| `offline` | 2 (offline) |
| anything else | not understood |

`data` is not read. Device status 1/2 is the same code the HTTP printer record
calls `device_status`; `printer_online` = device status 1 (BEH §1.1).

```json
{"type": "lastWill", "action": "onlineReport", "state": "offline",
 "code": 200, "msg": "done", "msgid": "<MSGID>", "timestamp": 0, "data": null}
```
*Constructed.*

#### 4.2 `status` — free/busy

`action` `workReport`: `state` `free` → work status 1 (free/available);
`busy` → work status 2 (busy). Other states not understood. `data` not read.
Work status is the HTTP record's `is_printing`. When no source has said
anything, 2.x treats work status as free for mode decisions (BEH §1.1).

```json
{"type": "status", "action": "workReport", "state": "busy",
 "code": 200, "msg": "done", "msgid": "<MSGID>", "timestamp": 0, "data": null}
```
*Constructed.*

#### 4.3 `user` — binding

`bindQuery`/`done` → the printer is marked bound to the account; `unbind`/`done`
→ marked unbound. Kept internally only; 2.x exposes nothing from it. `data` not
read. Context: switching a printer to LAN Mode removes it from the account.

#### 4.4 `print` — the job

Cloud: the job object comes from HTTP (the latest entry in the job list plus
its detail call, BEH §1.3); `print` reports update it live.

**Task-id rule.** Let `task` = `data.taskid` if `data` is an object with that
key, else "none". A report updates the job only if a job is known **and**
(`task` is none, or not a number, or negative, or equals the known job's id).
`taskid` may be an integer or a digit string. For any other task id the report
is not applied to the job and its `data` is discarded silently (no warning);
the work-status change in the table below **is** still applied. The new job is
picked up by the next HTTP poll (≤ 60 s; BEH G9) — and the "print started"
callback (§6.12) forces one 5 s after the printer turns busy.

**Action/state table** (job status codes as in BEH §1.3 / LAN §6.2):

| `action` | `state` | Work status | Job status set to | Other effects |
|---|---|---|---|---|
| `start` | `downloading` | busy | 4 downloading | printer-level download % = `data.progress` (**always**, even for an unmatched task); job download % = same (matched task only) |
| `start` | `checking` | busy | 5 checking | printer-level download % reset to 0 |
| `start` | `preheating` | busy | 6 preheating | apply job fields |
| `start` | `printing` | busy | 1 printing | apply job fields |
| `start` | `finished` | free | 2 complete | apply job fields |
| `pause` | `pausing` or `paused` | busy | 1 printing | pause flag 1; apply job fields |
| `resume` | `resuming` | busy | 1 printing | pause flag stays 1; apply job fields |
| `resume` | `resumed` | busy | 1 printing | pause flag 0; apply job fields |
| `start` or `stop` | `stopping` or `stoped` (sic) | free | 3 cancelled | apply job fields |
| `start` or `stop` | `failed` | free | 3 cancelled | job's failure reason = top-level `msg`; then 2.x logs an ERROR `Print Failed: <msg>` |
| `start` or `update` | `updated` | unchanged | unchanged | see "updated" below |
| `getSliceParam` | `done` | unchanged | unchanged | job slice parameters = `data.slice_param` (JSON text or object) |
| anything else (incl. `stop`/`stopped`, `pause`/`failed`) | | | | not understood |

Every status-setting row except `downloading` also resets the printer-level
download % to 0. Job status is set **from the action/state pair**, never from a
numeric field: 2.x's cloud path does not read `data.print_status`. The "`0` is
not a status — keep the previous one" rule (LAN §6.2; BEH B4) lives only in the
LAN `info.project` path in 2.x; a reimplementation should apply it wherever a
numeric job status arrives.

A job that HTTP last saw as complete (2) or cancelled (3) is never moved back by
a later HTTP refresh of the **same** job, but a pushed `print` report can move
it (BEH §1.3).

**Job fields applied** ("apply job fields"; each only if present):

| Field | Type / unit | Effect |
|---|---|---|
| `taskid` | int or digit string | matching only (above) |
| `localtask` | string (UUID) | ignored |
| `progress` | int, % | job progress |
| `curr_layer` | int | current layer |
| `total_layers` | int | total layers |
| `print_time` | int, minutes elapsed | elapsed time |
| `remain_time` | int, minutes remaining | remaining time |
| `filename` | string | job file name |
| `supplies_usage` | int, **millimetres** of filament extruded (LAN §6.2 correction) | stored on the job **only if the job's HTTP detail already carried a non-zero `supplies_usage`** (2.x quirk) |

Any other key in `data` → unhandled-data WARNING (state still applied).

**`updated` body** (`action` `start` or `update`):

| Field | Effect |
|---|---|
| `data.curr_hotbed_temp` + `data.curr_nozzle_temp` | current temperatures — only when **both** are present |
| `data.settings.fan_speed_pct` | printer's part-fan % |
| `data.settings.print_speed_pct` | printer's print-speed % (**only** reported here; LAN §7.2 note) |
| `data.settings.print_speed_mode` | printer's speed-mode integer |
| `data.settings.target_hotbed_temp` + `…target_nozzle_temp` | the job's target temperatures — only when **both** present and the task matches |

Any of these may be missing; a progress-only update carries no temperatures.

**Null rules:** `data: null` is tolerated for every status row except
`downloading` (which needs `data.progress`); the status/work-status change is
still applied and no job fields change. `settings: null` is treated as empty.

```json
{"type": "print", "action": "start", "state": "printing",
 "code": 200, "msg": "done", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"taskid": "<TASK_ID>", "localtask": "<LOCALTASK_UUID>",
          "filename": "benchy.gcode", "progress": 42, "curr_layer": 57,
          "total_layers": 136, "print_time": 31, "remain_time": 44,
          "supplies_usage": 5210}}
```

```json
{"type": "print", "action": "update", "state": "updated",
 "code": 200, "msg": "done", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"taskid": "<TASK_ID>", "curr_hotbed_temp": 60, "curr_nozzle_temp": 219,
          "settings": {"fan_speed_pct": 100, "print_speed_pct": 100,
                       "print_speed_mode": 2, "target_hotbed_temp": 60,
                       "target_nozzle_temp": 220}}}
```

```json
{"type": "print", "action": "stop", "state": "failed", "code": 10111,
 "msg": "Task abnormally ended", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"taskid": "<TASK_ID>"}}
```
*All three constructed from the parser's reads.*

#### 4.5 `tempature` (sic) — temperatures

Same body as LAN §6.3. Cloud pushes use `action: auto`; LAN replies use
`query`. Both require `state: done`.

| Field | Type / unit | Effect |
|---|---|---|
| `curr_hotbed_temp`, `curr_nozzle_temp` | int, °C | current temperatures — applied only when **both** are non-null |
| `target_hotbed_temp`, `target_nozzle_temp` | int, °C | printer-level set-points, each applied if non-null; **and** copied to the current job if one exists (2.x requires both non-null there, else it raises — ERROR log) |
| `curr_chamber_temp`, `target_chamber_temp` | number, °C, optional | kept raw; 0 on chamberless models (Kobra S1) |

`data: null` → handler fails (ERROR log), nothing applied (2.x gap; tolerate it).

```json
{"type": "tempature", "action": "auto", "state": "done", "code": 200,
 "msg": "done", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"curr_hotbed_temp": 60, "curr_nozzle_temp": 218,
          "target_hotbed_temp": 60, "target_nozzle_temp": 220}}
```
*Constructed; LAN capture in LAN §6.3.*

#### 4.6 `fan`

Same body as LAN §6.4; cloud `action: auto`, `state: done`.

| Field | Type | Effect |
|---|---|---|
| `fan_speed_pct` | int 0–100 | printer's part-fan % (the printer's figure, never the sliced job's) |
| `aux_fan_speed_pct` | int 0–100 | auxiliary part fan |
| `box_fan_level` | int level | enclosure/box fan level (a printer property, not per ACE) |

Each applied only if present; missing ≠ 0. `data: null` → ERROR in 2.x.

#### 4.7 `light`

Same bodies as LAN §6.5. Any `action`; `state` must be `done` (else not
understood). `data` empty or null → ignored quietly.

- Reply to a query (order 1232): `data.lights` = list of `{type, status, brightness}`.
- Reply to a control (order 1233) and pushed changes: `data` = one `{type, status, brightness}`.

Per light `type` (integer; Kobra S1 uses **2**): `status` 1 on / 0 off (null → 0),
`brightness` 0–100 (null → 0). 2.x keeps a map type → {status, brightness};
the "light type" of the printer is the lowest reported type. A light exists
only once one has been reported (then remembered across restarts; BEH §3.13).

```json
{"type": "light", "action": "query", "state": "done", "code": 200,
 "msg": "done", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"lights": [{"type": 2, "status": 1, "brightness": 100}]}}
```
*Shape from the library's own comment and LAN §6.5.*

#### 4.8 `peripherie` (sic) — fitted peripherals

Same body as LAN §6.9. `action` `query`, `state` `done` (reply to order 1231).
Keys `camera`, `multiColorBox` (the ACE), `udisk` (USB stick), each 1/0 →
stored as booleans; each only if present. This is the only way 2.x learns
whether a camera exists. `data: null` → ERROR in 2.x. History: an early note
records a Kobra S1 "not answering 1231" over the cloud; at that time 2.x sent
1231 in the wrong shape (integer `order_id` plus `project_id`), which the cloud
acknowledges and silently drops (D §0.2). The bare printer-level shape is what
the slicer sends.

```json
{"type": "peripherie", "action": "query", "state": "done", "code": 200,
 "msg": "done", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"camera": 1, "multiColorBox": 1, "udisk": 1}}
```
*Body captured on LAN (Kobra S1); same kind over cloud.*

#### 4.9 `axis` — head position and moves

Same bodies as LAN §6.6.

| `action` / `state` | `data` | Effect |
|---|---|---|
| `query` / `done` | `{"coordinates": {"x": mm, "y": mm, "z": mm}}` | head position (floats). If `coordinates` is missing or empty (Kobra X mid-print), the last position is kept. `data: null` → ERROR in 2.x. |
| `move` / any state | `null` | "move state" = the state text: `doing` moving; `done` finished; `failed` refused (usually axis not homed; Z is not covered by home-all). No position is carried. |
| anything else (e.g. `turnOff`) | | not understood |

Moving = a move state exists and is neither `done` nor `failed`; refused =
`failed` (BEH §1.8).

```json
{"type": "axis", "action": "query", "state": "done", "code": 200,
 "msg": "done", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"coordinates": {"x": 47, "y": 276, "z": 3.8152532726237904}}}
```
*`data` captured verbatim from a Kobra S1 reply to order 1214; envelope constructed.*

#### 4.10 `multiColorBox` — ACE

Box object and slot object are the same as LAN §6.7 (and as the HTTP printer
record's `multi_color_box`; 2.x parses all three with one model).

**Box object**

| Field | Type / unit | Required | Notes |
|---|---|---|---|
| `id` | int, 0-based box index | **yes** (a box without it is rejected; never invent a box) | |
| `status` | int | no (default 0) | |
| `model_id` | int | no (0) | 40001 = ACE Pro; 40002 another ACE (name unconfirmed) |
| `auto_feed` | 0/1 | no (0) | run-out refill |
| `loaded_slot` | int, 0-based, −1 none | no (−1) | may read −1 mid-print while a slot's `status` is 5 (use that slot); reverts to −1 when a job ends |
| `feed_status` | object or null | no | below |
| `temp` | int, °C | no (0) | |
| `humidity` | number | no (null) | 0 without a sensor |
| `drying_status` | object or null | no | below |
| `curr_nozzle_temp`, `target_nozzle_temp` | int | no | stored, not exposed |
| `slots` | list | no (empty) | below |

Null in any optional field is coerced to its default (never raises) — BEH B3.

**Slot object** (2.x is strict here: a null in a required slot field raises)

| Field | Type | Required | Notes |
|---|---|---|---|
| `index` | int, 0-based | yes | |
| `sku` | string | yes | |
| `type` | string (material) | yes | |
| `color` | [r, g, b] ints | yes | |
| `edit_status` | int | yes | 0 from tag, 1 by hand, **2 slot empty** (the ACE keeps reporting the old material) |
| `status` | int | yes | 5 = loaded into the printer, 4 = not |
| `color_group` | list of RGBA lists | no | several entries = multi-colour spool |
| `icon_type` | int | no | |
| `consumables_percent` | number | no | always 0 observed; show, never use |

**`feed_status`**: `code` (int, 200 ok, default −1), `type` (1 feed, 2 retract,
3 finish, −1 idle), `current_status` (int), `slot_index` (0-based, −1 none).
**`drying_status`**: `status` (1 = drying, else not), `target_temp` °C,
`duration` min, `remain_time` min (all default 0; while not drying, 2.x reports
target/duration/remaining as 0).

**Actions** (state words per LAN Q1; `success` and `done` both mean completed):

| `action` | `state` | Body read | Effect |
|---|---|---|---|
| `getInfo` | `success` | `data.multi_color_box` (list, or a single box object); `data.head_tools_model` (ignored) | replace the box list — **merge rule**: if fewer boxes are reported than are known, update the reported ones by `id` and keep the others; if the same or more, replace wholesale; `null` removes all |
| `setInfo`, `refresh` | `success` | `data.multi_color_box[]`: `id`, `slots[]` (full slot objects) | for each box: replace each reported slot at position `index` |
| `autoUpdateInfo` | `done` | **flat** `data.id`, `data.loaded_slot` (2.x reads these directly, not a list) | set that box's loaded slot |
| `autoUpdateDryStatus`, `setDry` | `success` | `data.multi_color_box[]`: `id`, `temp`, `drying_status` | set box temperature and drying status |
| `feedFilament` | `done` | `data.multi_color_box[]`: `id`, `loaded_slot`, `feed_status` | set loaded slot and feed status |
| `setAutoFeed` | `done` | `data.multi_color_box[]`: `id`, `auto_feed` | set run-out refill |
| other | | | not understood |

2.x quirks to decide on: boxes are addressed by **list position = `id`**; a
report naming a box id beyond the number of known boxes is skipped (a box is
only created by `getInfo` or the HTTP record). Two boxes are reachable by
accessors, 3–4 are parsed but unreachable (notes: Kobra S1 takes 2 ACE Pro,
Kobra X up to 4).

```json
{"type": "multiColorBox", "action": "getInfo", "state": "success", "code": 200,
 "msg": "done", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"multi_color_box": [
   {"id": 0, "status": 1, "temp": 33, "humidity": 0.0, "model_id": 40001,
    "auto_feed": 1, "loaded_slot": -1,
    "feed_status": {"code": 200, "type": -1, "current_status": -1, "slot_index": -1},
    "drying_status": {"status": 0, "duration": 0, "target_temp": 0, "remain_time": 0},
    "slots": [{"index": 0, "sku": "", "type": "PLA", "color": [255, 255, 255],
               "status": 5, "edit_status": 0}]}]}}
```
*Box shape as a Kobra S1 sends it (quoted in the library tests); envelope constructed.*

```json
{"type": "multiColorBox", "action": "autoUpdateDryStatus", "state": "success",
 "code": 200, "msg": "done", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"multi_color_box": [{"id": 0, "temp": 44,
   "drying_status": {"status": 1, "target_temp": 45, "duration": 240, "remain_time": 212}}]}}
```
*Constructed.*

#### 4.11 `extfilbox` — external filament holder

Same body as LAN §7.2 ("reports that carry more"). `action` `reportInfo`,
`state` `success`. Fields read: `type` (material text), `color` [r,g,b],
`loaded` 0/1, `status_type` int, `current_status` int; all null-tolerant. The
holder's `id` is **not** read from MQTT (only from HTTP; an `id` key here
produces an unhandled-data warning). Applied only if the HTTP printer record
already produced a holder; an all-null HTTP holder means **no holder**
(BEH B34), in which case these reports are not applied (2.x then logs the
unread body as an unhandled-data warning).

#### 4.12 `file` — printer storage listings

Cloud-only; replies to HTTP orders 101–104 (D §3.3–3.4).

| `action` / `state` | Body | Effect |
|---|---|---|
| `listLocal` / `done` | `data.records[]` | replace the internal-storage list |
| `listUdisk` / `done` | `data.records[]` | replace the USB list |
| `deleteLocal` / `success`, `deleteUdisk` / `success` | — | acknowledged, no state (2.x re-requests the list afterwards) |
| `cloudRecommendList` / `done` | anything | whole payload ignored |

Record: `filename` (string, required), `timestamp` (int, optional, default 0),
`size` (int bytes, optional), `is_dir` (bool, required). 2.x exposes name and
size in MB (size / 10⁶). `data.records: null` leaves the previous list; an
empty list reads as "no value" in 2.x (BEH G12).

#### 4.13 `ota` — firmware (printer and ACE)

Cloud-only. Updates are started by HTTP (D §4); progress arrives here.

Printer (`ota` on a topic **without** `multiColorBox`):

| `action` / `state` | Body | Effect |
|---|---|---|
| `reportVersion` / `done` | `data.firmware_version` (string); `device_unionid`, `machine_version`, `peripheral_version`, `model_id` (ignored) | if the version differs from the known one: store it, clear "updating"/"downloading", zero both progress figures, clear "update available" |
| `update` / `start` | — | updating = true |
| `update` / `downloading` | `data.progress` int % | updating, downloading, download % |
| `update` / `updating` | `data.current_progress` int % | updating, not downloading, install % |
| other (incl. `update-success`) | | not understood |

ACE (`ota` on a topic **containing** `multiColorBox`; box = segment 9, default
0): the same rows, plus `update` / `update-success` and
`update` / `updateSuccessProcessed` accepted and ignored. If the HTTP record
listed no firmware entry for that box, the message is ignored.

Combined progress shown by 2.x (BEH §2.17): while updating or downloading —
if install % = 0 and download % > 0: `min(download / 2, 100)`; else if install
% > 0: `min(install / 2 + 50, 100)`; else `1`. Not updating → "not in progress".

#### 4.14 `aiSettings`

Same body as LAN §6.8: settings nested under `data.ai_settings` (`status`,
`type`, `count`, `notice_type` list, `sensitivity_level` list). Any action and
state. Stored wholesale; a report without it keeps the previous settings.
Enabled = `status` non-zero. Over the cloud it only arrives when the printer
volunteers it (e.g. after a change); 2.x never asks for it over the cloud.

#### 4.15 `event`, `printerevent`, `printer_event` — faults

Any action and state. The fault code is taken from the **envelope** `code`
(§3.2). Inside `data`, the keys `code`, `msg`, `msgid`, `state`, `action` are
consumed without being used; the real nested shape has not been captured.
This kind was added because a Kobra X filament run-out was being discarded as
"not understood" (#21).

```json
{"type": "event", "action": "report", "state": "done", "code": 10107,
 "msg": "filament", "msgid": "<MSGID>", "timestamp": 0,
 "data": {"code": 10107, "msg": "filament", "msgid": "<MSGID>", "state": "s", "action": "a"}}
```
*Constructed (library test shape).*

#### 4.16 `info`

The whole-printer snapshot of LAN §6.1. The library states that **only the LAN
connection sends it**; on the cloud the same ground is covered by `status`,
`tempature`, `fan` and `print`. If it did arrive, 2.x would treat it exactly as
on LAN (including setting device status 1).

#### 4.17 `video`

Not handled (not understood). `video`/`startCapture` is a **LAN** command
(LAN §7.2); publishing it on the cloud broker was tested with the link
confirmed up and the printer did nothing. The cloud camera uses HTTP order
1001 and Agora (D §1); video never travels over MQTT (captured: nothing over
1.5 KB during a live feed).

#### 4.18 User topics (U1, U2)

`.../server/app/<USER_ID>/<USER_ID_MD5>/slice/report` and `…/fdmslice/report`
carry the account's cloud-slicing reports. 2.x logs them at debug and does
nothing else. Body not documented.

#### 4.19 Captured traffic inventory

A 60-second capture on every permitted topic, with a print running and the
slicer's live camera open, contained 26 messages of kinds: `fan`,
`multiColorBox`, `tempature`, `status`, `print`, and `response`-topic messages.
Nothing exceeded 1.5 KB.

#### 4.20 Null-payload rules (summary for the rewrite)

1. A key **present with value null** must be treated like an absent key; a
   default that only applies to absent keys is not a defence (firmware 2.0.1.9
   nulled `external_shelves` fields and 2.x lost the whole printer, #28).
2. `data: null` must be tolerated for every kind. 2.x tolerates it for `print`
   status rows, `light`, `axis`/`move`, `info`, `aiSettings`, `event`; it fails
   (ERROR log, nothing applied) for `tempature`, `fan`, `peripherie`,
   `axis`/`query`, `multiColorBox`, `file`, `print`/`downloading`.
3. An ACE box needs an `id`; everything else defaults. A holder with no id, no
   type and no loaded flag is absent, not empty.
4. A nested block with unknown keys must not fail the report (BEH B3).
5. Job status `0` or non-numeric = not a status; keep the previous one (§4.4).

---

### 5. Commands and their transport

#### 5.1 Principle

**2.x publishes nothing on the cloud broker.** Every command is an HTTP order
(`POST /work/operation/sendOrder`, D §0.2) or an HTTP firmware call; CMQTT only
carries the resulting reports and acknowledgements (notes: "capturing MQTT to
find commands is a dead end"). When the LAN link is up, orders that have a
local form are published to the printer's own broker instead (LAN §7.2), decided
per send; orders wanting the raw HTTP reply (camera open) never divert.

#### 5.2 Per command (2.x)

"Wakes" = 2.x brings CMQTT up before sending (§6.9). Order payloads: D §0.2 and
the per-topic sections of D; LAN equivalents: LAN §7.2.

| 2.x control | Order / call | Cloud transport | LAN form (when LAN is up) | Expected CMQTT reply | Wakes |
|---|---|---|---|---|---|
| Start print (actions) | 1 | HTTP | — | `print`/`start` … | no |
| Pause / resume / cancel buttons | 2 / 3 / 4 | HTTP | `print` `pause`/`resume`/`stop` | `print` pause/resume/stop rows | yes |
| Job print settings (numbers/selects while printing) | 6 | HTTP | `print`/`update` | `print`/`update`/`updated` | no |
| Target temperatures (numbers) | 1216 | HTTP | `tempature`/`set` | `tempature` report (inferred) | no |
| Fan speeds (numbers) | 1221 | HTTP | `fan`/`setSpeed` | `fan` report (inferred) | no |
| Light on/off | 1233 | HTTP | `light`/`control` | `light` single-object | yes |
| Light status query (capability poll) | 1232 | HTTP | `light`/`query` | `light` list | no (only sent while CMQTT is up) |
| Peripherals query (capability poll) | 1231 | HTTP | `peripherie`/`query` | `peripherie`/`query` | no (same) |
| Axis jog / home buttons | 201 | HTTP | `axis`/`move` | `axis`/`move` doing→done/failed | no |
| Disengage motors | 1213 | HTTP | `axis`/`turnOff` | not handled | no |
| Request axis position | 1214 | HTTP | `axis`/`query` | `axis`/`query`/`done` | yes |
| ACE refresh spools | 1206 | HTTP | `multiColorBox`/`getInfo` | `multiColorBox`/`getInfo`/`success` | yes |
| ACE drying start (preset buttons) | 1207 | HTTP | `multiColorBox`/`setDry` | `setDry`, then `autoUpdateDryStatus` | yes |
| ACE drying start (plain start buttons) | 1207 | HTTP | same | same | **no** |
| ACE drying stop (box 1 stops every box; box 2 only box 2) | 1207 | HTTP | same | same | yes |
| ACE feed / retract (buttons, actions) | 1208 | HTTP | `multiColorBox`/`feedFilament` | `feedFilament`/`done` | no |
| ACE set slot (actions) | 1211 | HTTP | `multiColorBox`/`setInfo` | `setInfo`/`success` | no (BEH G16) |
| ACE run-out refill switch | 1212 | HTTP | `multiColorBox`/`setAutoFeed` | `setAutoFeed`/`done` | yes |
| AI detection switch | 1243 | HTTP (cloud only by Anycubic's design) | none | `aiSettings` if volunteered | no |
| List local / USB files (buttons) | 103 / 101 | HTTP | none | `file`/`listLocal`/`listUdisk` | yes |
| Delete local / USB file (actions) | 104 / 102 | HTTP | none | `file`/`delete…`/`success` | no (BEH G16) |
| Cloud file list button | HTTP file list | HTTP | — | none (HTTP reply) | **yes** (quirk: nothing arrives over MQTT) |
| Printer / ACE firmware update | HTTP firmware endpoints | HTTP | — | `ota` update/reportVersion | yes |
| Cloud camera open | 1001 | HTTP (raw reply) | — | none | no |
| LAN camera start | — | — | `video`/`startCapture` | — | — |

Note (notes, captured): the cloud answers `{"code": 1, "msg": "Operation
successful"}` even for orders it silently drops; only the CMQTT report proves an
order took effect.

---

### 6. Lifecycle in 2.x

#### 6.1 Preconditions

CMQTT is only ever attempted when **all** hold: the entry has a cloud account
(not LAN-only); the auth mode supports MQTT login (Slicer or Android, never
Web); `mqtt_connect_mode` ≠ 5. The diagnostic binary sensor
`mqtt_connection_active` carries `supports_mqtt_login` and `last_error`
attributes (BEH §2.1).

#### 6.2 `mqtt_connect_mode` (options flow "MQTT Settings"; absent = 1)

| Value | Label | Link is wanted ("active") when | Idle ("inactive") when |
|---|---|---|---|
| 1 | Printing Only | any printer's work status is busy | every printer is not busy **and** has no job in progress |
| 2 | Printing & Drying | any printer busy, **or** any printer's first or second ACE is drying | no printer busy, no job in progress, nothing drying |
| 3 | Device Online | any printer is online (device status 1) or busy | no printer online or busy |
| 4 | Always | always | never |
| 5 | Never Connect | never — not even for actions or the manual switch | — |
| any other value | — | never (unless manual switch or a recent action) | never (so once up it is never released for idling) |

"Job in progress" = job status 1, 4, 5 or 6, or an unknown code whose text phase
is not a terminal word (BEH §1.3). The asymmetry in mode 1 (start on busy only;
release needs also no job in progress) is deliberate.

#### 6.3 Reasons that override the mode (except mode 5)

- **Recent action.** Every "wake" (§6.9) stamps `last_action = now`. For
  **300 s** after it, the link counts as active in every mode and as not
  inactive (the idle clock cannot start).
- **Manual switch** `manual_mqtt_connection_enabled`: while on, start is allowed
  and release for idling is suppressed. Held in memory only: it resets to off
  on every reload/restart. Turning it on or off triggers an immediate full
  refresh (§6.4), which runs the connect check.

#### 6.4 When the connect/disconnect check runs

1. At the end of each **successful** cloud HTTP poll (the poll runs at most
   every 60 s inside a 15 s coordinator tick; it is skipped while the LAN link is
   connected and never runs for LAN-only entries).
2. On every "wake" (§6.9).
3. From the refresh button (§6.10).

Every control ends with a forced full refresh (which includes a cloud poll and
therefore the check), after which the next cloud poll is due ~10 s later.
A check is skipped while a refresh-button cycle holds its lock; checks are
serialised.

Start is refused while Home Assistant is not in the *running* state or is
stopping. While the LAN link is up the check never runs, so an already open
CMQTT link is neither released nor refreshed (see Open points).

#### 6.5 Start sequence

Start happens when: not already started, HA running and not stopping,
preconditions hold, and (mode active **or** manual switch on).

1. Put every loaded printer into the subscription set.
2. If no connect task is outstanding, run the connect on a worker thread:
   create the client (client id §1.4, clean session), compute username and
   password (§1.5), build the TLS context (§1.2), set the reconnect delay
   (5 s min), connect to host:port with keep-alive 1200 s, then run the
   blocking network loop.
3. On CONNACK 0: subscribe U1, U2, then P1+P2 per printer (§2.2); fire the
   "subscribed" callback.
4. First SUBACK → the link counts as connected for waiters (§6.9).
5. 10 s after the "subscribed" callback, for every printer that is online,
   send HTTP orders 1231 (peripherals) and 1232 (light status); replies arrive
   over CMQTT. This repeats on **every** reconnect.
6. The client object exists from step 2 until the loop ends; the sensor
   `mqtt_connection_active` is **on** whenever it exists — connected, still
   connecting, or looping through reconnect attempts.

#### 6.6 Idle release and the stop sequence

Stop happens when started and (HA stopping, or (idle **and** manual switch off)).

Idle clock: at a check where the mode's "inactive" condition holds, if no clock
is running it starts (`idle_since = now`); at a later check where it still holds
and `now > idle_since + 900 s`, the link is idle (and the clock resets). Any
check where "inactive" does not hold clears the clock. Because checks run at
cloud polls, release happens at the first check after 15 min of continuous
inactivity (≈ 15–16 min). The clock keeps running while the manual switch is
on, so turning the switch off releases the link within ≤ 15 min.

Stop sequence:
1. Unsubscribe P1/P2 for each printer; empty the subscription set.
2. Send MQTT DISCONNECT (graceful).
3. Wait up to 10 s for the disconnect to be confirmed.
4. Cancel the connect task if still running; clear it.

Unload of the config entry stops the link the same way (then closes LAN). There
is no dedicated HA-stop listener; the "HA stopping" clause applies to checks
that happen to run during shutdown.

#### 6.7 Failure handling

| Failure | What happens in 2.x | Retry |
|---|---|---|
| Error before/while connecting (DNS, TCP refused, TLS handshake, certificate missing, client id cannot be built) | the connect call raises; the client object is discarded (sensor off); the task ends with the exception; `last_error` = `<ExceptionType>: <message> (host=<host>:<port>)`; one ERROR log "Anycubic MQTT connection failed" | next check (≤ ~60 s); a failed attempt never blocks later ones (BEH B12) |
| CONNACK refused (e.g. rc 5, not authorised — stale/revoked token) | WARNING `Failed to connect, return code <rc>`; the broker closes; the client stays alive and reconnects automatically with backoff 5, 10, 20, 40, 80, 120, 120 … s, recomputing credentials each time | **indefinitely**; sensor stays on; `last_error` stays empty (see Open points) |
| Clean task end (after a deliberate stop) | debug log; `last_error` cleared | — |

#### 6.8 Unexpected disconnect and reconnect

Any disconnect not requested by 2.x (network loss, keep-alive expiry, the
broker dropping the session because another client connected with the same
client id — §6.15):

1. Recompute username/password from the current token.
2. The client library reconnects on its own after 5 s (doubling on consecutive
   failures, cap 120 s, reset after a successful CONNACK).
3. On CONNACK 0: resubscribe everything; the "subscribed" callback fires again
   (capability queries 10 s later).

2.x itself does nothing else; the connection sensor stays on throughout.

#### 6.9 "Wake" — bringing the link up for an action

Sequence used by the controls marked "wakes" in §5.2:

1. `last_action = now` (holds the link for 300 s, §6.3).
2. Run the connect check (§6.4–6.5).
3. Wait for the link: if no connection is pending confirmation (never started,
   or already confirmed earlier) → proceed at once; otherwise wait up to
   **10 s** for the first SUBACK, then a further **2 s** settle delay.
4. Timeout → Home Assistant error with translation key `mqtt_connect_timeout`
   ("Timed out connecting to the printer. Try turning on the manual MQTT
   connection switch, then retry.") and the order is **not** sent.
5. With no CMQTT possible (Web token, LAN-only, mode 5) step 3 succeeds at once
   and the order is sent anyway.
6. 2.x quirks (Open point 17): the "pending" marker is only created when the
   connect routine starts on its worker thread, so a wake right after a clean
   release can proceed before the link exists; after a failed connect the
   marker stays pending, so wakes time out after 10 s until a connect succeeds.

A constant for a 12 s camera connect timeout exists but is unused.

#### 6.10 Refresh MQTT connection button

- Rate limit: at most once per **300 s**; presses inside the window, or while a
  connect check is running, are ignored silently.
- If started: run the stop sequence (§6.6), then wait **2 s**.
- If not started: discard any leftover failed task.
- Run the connect check, which reconnects **only if** the mode, the manual
  switch or a recent action calls for it (the button does not itself count as
  an action). So in Printing Only mode on an idle printer the button
  disconnects and does not reconnect.
- No forced refresh afterwards.

#### 6.11 Local file-list retry

After the "request local file list" button: 5 s later, if the printer is online
and **both** the list before the press and the list now are empty, 2.x runs the
refresh-button logic (subject to its rate limit), waits for the link (§6.9
step 3), waits 2 s and sends order 103 once more.

#### 6.12 How a CMQTT message drives refreshes

- **Every** printer-topic message that reached step 6 of §2.5 → the
  coordinator's state map is rebuilt from the in-memory printer objects, the
  refresh is marked **successful**, listeners are notified, and printer
  capabilities (light types) are persisted. No HTTP call is made. (Notes flag
  that this marks the coordinator successful even while cloud polls are
  failing, resurrecting stale entities — gap.)
- **Print started**: if a message moves a printer from work status 1 (free)
  to 2 (busy), a full forced refresh (including HTTP printer record and job
  list) runs **5 s** later — this is how a new job's id becomes known so its
  `print` reports stop being discarded. A transition from "unknown" does not
  trigger it.
- User-topic messages and dropped messages trigger nothing.

#### 6.13 How HTTP polling and CMQTT state combine

Both write the same in-memory printer; the last writer wins. The HTTP poll
(≤ every 60 s) rewrites: device status, work status, current temperatures,
firmware records, the ACE list (wholesale), the external holder, tools, and the
job (status, progress, times, target temperatures and limits from the job
detail). CMQTT alone writes: part/aux/box fan, printer-level print-speed % and
mode, printer-level target temperatures, chamber temperatures, lights,
peripherals, head position and move state, local/USB file lists, AI settings,
fault code, download %, bound flag, firmware progress. Consequence: between
polls CMQTT is fresher; at each poll HTTP may briefly overwrite pushed values
with the cloud's cached view.

#### 6.14 Capability polling

BEH §3.14 is exact: 10 s after each subscribe, and at the end of each
successful cloud poll while the link is up, orders 1231/1232 go to online
printers still missing either answer, at most 3 times per printer, the count
restored when a printer comes back online. It never opens CMQTT by itself,
because doing so would take the account's single session from the slicer.

#### 6.15 Session displacement

Facts and observations:

- **Shared MQTT identity.** The Slicer-mode client id is exactly the one the
  Anycubic slicer uses (`md5(identity + "pcf")`); the Android-mode id is the
  phone app's (`md5(identity)`). MQTT brokers drop an existing session when a
  new CONNECT arrives with the same client id; notes state the broker "validates
  the client id too, so only one session per identity". Expected symptom: while
  2.x holds CMQTT, the user's slicer (or app) loses live status, and vice versa;
  in 2.x this shows as repeated "unintentionally disconnected, will reconnect"
  debug lines and reconnects every ≥ 5 s. This is why the default mode is
  Printing Only and capability polling never opens the link.
- **Counter-observation.** One reporter runs the slicer and 2.x side by side
  mid-print "with no trouble" (auth mode not recorded). Displacement is
  therefore not proven for every setup (see Open points).
- **HTTP session revocation** (separate but related): signing in with the
  slicer or phone app can revoke the session 2.x holds, server-side, on both
  regions ("Login information has expired. Please login again."). CMQTT
  credentials derived from a revoked token are refused (CONNACK rc 5), which in
  2.x becomes the endless reconnect loop of §6.7; HTTP then asks for
  re-authentication.
- **Camera**: order 1001 answers "Operation successful" without credentials when
  another session is the account's most recent; 2.x retries once behind a fresh
  login (D §1.5).
- **Research tip**: to capture CMQTT by hand, park 2.x first with
  `mqtt_connect_mode` 5 and connect within seconds of deriving credentials.

#### 6.16 Logging and debug options

- Option `debug_mqtt_msg` (default: the deprecated `debug` option): logs every
  processed printer message at debug with the printer key redacted.
- Always: user-topic messages at debug; subscribe/connect/disconnect at debug;
  CONNACK refusals at WARNING; connect failures at ERROR (with host:port).

#### 6.17 Timing constants

| Constant | Value |
|---|---|
| Coordinator tick | 15 s |
| Cloud HTTP poll (includes the connect check) | ≤ every 60 s |
| After a control: next cloud poll | ~10 s |
| Keep-alive | 1200 s |
| Reconnect backoff | 5 s doubling to 120 s |
| Wait for link (wake) | 10 s + 2 s settle |
| Stop: wait for disconnect | 10 s |
| Action hold | 300 s |
| Idle release | 900 s continuous |
| Refresh button rate limit | 300 s; 2 s pause between stop and start |
| Capability queries after subscribe | 10 s; max 3 per printer |
| Forced refresh after "print started" | 5 s |
| Local file-list retry | 5 s, then 2 s |

---

### 7. Open points

1. **China broker**: host reported by one user; port 8883 assumed; client id
   `md5(mobile + "pcf")` unconfirmed; whether hostname-off is still needed.
2. **Android-mode login**: no note confirms that Android-mode credentials
   (bcrypt, cost 12, `$2b$`) are accepted by the broker; the maintainer uses
   Slicer mode. Whether the broker requires a particular bcrypt variant/cost is
   unknown.
3. **Why hand-derived credentials "go stale fast"** although nothing in them is
   time-based (token lifetime? server-side session check?). Unknown.
4. **Displacement**: whether the broker really evicts the slicer/app (same
   client id) — contradicted by one field report. Needs an A/B test per auth
   mode.
5. **Exact report topics**: the full cloud topic for each kind, what arrives on
   P1 (`printer/app/.../response`) versus P2, and the ACE `ota` topic layout
   (which segment holds the box index) are inferred, not captured.
6. **`timestamp` and `msgid`** semantics on the cloud (wall clock? echo of the
   HTTP order's `msgid`?) — the echo is claimed in the library's documentation,
   never verified or used.
7. **`print` over the cloud**: whether `data` carries `print_status`, `state`,
   `pause`, and whether `stop`/`stopped` (double p) and `pause`/`failed` occur
   on the cloud (2.x does not understand them). Whether `taskid` is an integer
   or a string.
8. **`autoUpdateInfo` shape**: 2.x reads flat `data.id`/`data.loaded_slot`,
   LAN Q1 describes "boxes with id and loaded_slot". Capture needed.
9. **Replies to 1216 / 1221 / 1213 / 1243**: kind/action of the report (if any)
   not captured.
10. **`event` nested `data`** shape never captured; `user`, `lastWill`,
    `status`, `ota`, `file` bodies are known only from the fields 2.x reads.
11. **Printers added while the link is up** are not subscribed until the next
    reconnect (2.x gap).
12. **CONNACK refused** (bad token) leaves the connection sensor on with no
    `last_error` and loops forever at ≤ 120 s intervals (2.x gap; BEH B13 only
    covers the raised-exception path).
13. **LAN connected with CMQTT open**: the check never runs, so the link is
    never released (2.x gap; practical impact small because a LAN-Mode printer
    leaves the cloud account).
14. **Cloud peripheral query**: no note records a cloud `peripherie` reply
    captured since 1231 was fixed to the bare shape; the one "no answer"
    observation predates that fix. Confirm on hardware.
17. **Wake race in 2.x**: after a clean release the "pending connection" marker
    is cleared, so a later wake can find nothing pending and send its order
    before the new connection has even been created; conversely, after a failed
    connect the marker stays pending, so every wake waits 10 s and fails with
    `mqtt_connect_timeout` until a connection succeeds. The rewrite should wait
    on the real link state.
15. **Licensing of the TLS material** (`anycubic_mqqt_tls_*`): Anycubic's own
    certificate and private key, carved out of the GPL grant; distribution
    rights for a new project are unresolved (BEH §9 V7).
16. **CN-only broker certificate**: hostname verification depends on CN
    fallback; a future OpenSSL/Python default that disables it would break the
    international connection.

---

## Part D — Cloud: camera, printing and files, firmware updates

Specification-team facts for the clean-room rewrite. Written from reading the
2.x integration (`anycubic_cloud` 2.9.4), the library it pins
(`anycubic-cloud-api` 0.4.32), their tests and fixtures, and the maintainer's
research notes (slicer traffic captures, the slicer's embedded web bundle).
No code is reproduced; behaviour is described in prose and tables.

**Do not duplicate — cross-references.** Entity-level presentation is already
specified in `hass-anycubic-next/docs/BEHAVIOUR.md`: §2.5 (file sensors),
§2.12 (buttons), §2.15 (AI switch), §2.17 (update entities and the progress
formula), §2.18 (both cameras), §4.4 (print actions and their fields), §4.5
(delete actions), §4.8 (event after a cloud print), §7 (2.x gaps G10, G12, G16,
G17), §9 (V6, V8, V17). The LAN camera and LAN reports are in
`anycubic-lan/docs/PROTOCOL.md` (§6.1 `info`, §6.8 `aiSettings`, §6.9
`peripherie`, §7). This document adds the **wire level underneath** those
entities and actions, and anything cloud-only they do not cover.

**Placeholders used in examples:** `<PRINTER_ID>` (integer cloud printer id),
`<PRINTER_KEY>` (printer key string), `<MACHINE_TYPE>` (model id, e.g. 20025),
`<USER_ID>`, `<MSGID>`, `<AGORA_APP_ID>`, `<RTC_TOKEN>`, `<CLIENT_UID>`,
`<PUBLISHER_UID>`, `<EVENT_ID>`, `<CHANNEL_KEY>`, `<KDF_SALT_B64>`,
`<PRESIGNED_PUT_URL>`, `<LOCK_ID>`, `<CLOUD_FILE_ID>`, `<GCODE_ID>`, `<URL>`.
No secret values appear anywhere in this document.

---

### 0. Conventions shared by every section

#### 0.1 HTTP

- All cloud calls go to the region's API root: `https://<base_domain>/p/p/workbench/api`
  followed by the endpoint path (international `base_domain` is
  `cloud-universe.anycubic.com`; the China value is in the sibling
  transport/auth spec). Authentication headers are those of the sibling
  transport/auth spec.
- `POST` bodies are a JSON document; `GET` parameters travel in the query string.
- 2.x does **not** check the envelope `code` on these calls in general; each
  call interprets `data` (and sometimes `msg`) itself, as described per call.

#### 0.2 Orders (`POST /work/operation/sendOrder`)

Printer commands are "orders": one HTTPS POST whose reply only acknowledges
receipt. The effect, and any data the printer sends back, arrives later over
the cloud MQTT link (CMQTT). **"Operation successful" never proves anything
happened** — orders with the wrong shape are acknowledged and silently dropped.

Four body shapes are in use in 2.x:

| Shape | Keys sent | `order_id` type | Used here by |
|---|---|---|---|
| Printer-level with payload | `order_id`, `printer_id`, `data` | **string** | 1216, 1221, 1243, 201, 1213 |
| Printer-level bare query | `order_id`, `printer_id` and **nothing else** | **string** | 1231 (peripherals), 1232 (light) |
| Camera open | `order_id`, `printer_id`, `shengwang_rtc_support: true` (no `data`, no `project_id`) | **string** | 1001 |
| Project-level | `order_id`, `printer_id`, `project_id`, `data` (and for order 1 also `ams_info`, `settings`) | **integer** | 1 (start print, `project_id` 0), 2/3/4/6 (job id), 101–104 (`project_id` 0) |

Reply: `{"code": <int>, "msg": <text>, "data": {"msgid": <text>, ...}}`.
2.x's generic order sender treats a null `data` as failure; if at the same time
`msg` is exactly `No file found` it raises a dedicated "file not found in the
cloud" error (used by the print retry, §2.5.4); otherwise it raises a generic
"send order failed: <msg>" error. A non-null `data` without `msgid` is logged
and returns "no message id" rather than failing.

#### 0.3 CMQTT reply envelope

Printer reports arrive on
`anycubic/anycubicCloud/v1/printer/public/<MACHINE_TYPE>/<PRINTER_KEY>/<type>[...]`
(and the `.../printer/app/...` mirror) with the envelope
`{type, action, state, code, msg, msgid, timestamp, data}`; `code` 200 means
"processed". Only `type`/`action`/`state`/`data` matter to this document.
Messages on a topic whose 8th segment (0-based index 7) is `response` and whose
payload has a single key are dropped unread.

---

### 1. Cloud camera

#### 1.1 What it is

The cloud camera is **Agora ("shengwang", 声网) WebRTC**. The Anycubic cloud does
not relay video; order 1001 makes the printer publish into an Agora channel and
hands the caller short-lived join credentials in the **HTTP reply** (not over
MQTT). Video never travels over MQTT (a 60 s capture with the slicer's feed live
showed nothing over 1.5 KB). The local HTTP-FLV camera on port 18088 is a
different, LAN-only mechanism (BEHAVIOUR §2.18, PROTOCOL §7); its start message
`video`/`startCapture` is ignored when published on the cloud broker.

#### 1.2 Does this printer have a camera?

Camera presence is **not** in the 1001 reply. Sources, in order of authority:

| Source | How | Value | Used by 2.x? |
|---|---|---|---|
| Peripherals query | order **1231**, bare-query shape (`{"order_id": "1231", "printer_id": <PRINTER_ID>}`). On LAN the same question is `peripherie`/`query`. | CMQTT reply `type: "peripherie"`, `action: "query"`, `state: "done"`, `data: {"camera": 1\|0, "multiColorBox": 1\|0, "udisk": 1\|0}` (ints observed on LAN; 2.x coerces each to a boolean, each key optional) | **yes** — the only source |
| Cloud printer record (`GET /v2/printer/info`) `type_function_ids` | 22 = FDM peer video, 7 = LCD (resin) peer video | list of ints | no |
| Cloud printer record `features` | list of `{name, value}`; `shengwang_rtc_support: true` says the printer speaks this camera protocol; `shengwang_rdt_support` also present (meaning unknown); `camera_timelapse_support` | booleans | no (the library reads `features` only from the LAN `info` report) |

When 2.x asks 1231: (a) 10 s after CMQTT subscribes, for every printer that is
online (together with 1232, the light query); (b) during every cloud refresh
while CMQTT is **already** up, for each online printer whose camera flag is
still unknown, at most **3 polls per printer** (one budget shared with the
light query, counted once per refresh that asks either); that budget is restored when the
printer is seen going from offline to online. 2.x never brings CMQTT up just to
ask. Once answered, the flag is kept for the life of the printer object.

```json
{"type": "peripherie", "action": "query", "state": "done", "code": 200,
 "msg": "done", "msgid": "<MSGID>",
 "data": {"camera": 1, "multiColorBox": 1, "udisk": 1}}
```

#### 1.3 Opening a stream — the request

`POST /work/operation/sendOrder` with exactly:

```json
{"order_id": "1001", "printer_id": <PRINTER_ID>, "shengwang_rtc_support": true}
```

Load-bearing details, each verified against real hardware; none produces an
error when wrong (the cloud answers "Operation successful" regardless):

- `order_id` must be the **string** `"1001"`.
- `shengwang_rtc_support: true` is a **top-level** field. Without it the reply's
  `msg` is `Video service upgraded. Update the slicer to enable.` and no
  credentials come back.
- No `data`, no `project_id`.
- This call always goes to the cloud; it has no LAN form and is never diverted.
- Issuing 1001 is itself what makes the printer join the channel and publish.
  There is no separate start command on the cloud path.

The slicer only offers the camera when the printer's state is busy or free
(other states are refused client-side before any request).

#### 1.4 The reply — every field

```json
{"code": 1, "msg": "Operation successful",
 "data": {
   "msgid": "<MSGID>",
   "token": "<opaque, unused>",
   "shengwang": {
     "appid": "<AGORA_APP_ID>",
     "channel": "<PRINTER_KEY>",
     "rtc_token": "<RTC_TOKEN>",
     "uid": <PUBLISHER_UID>,
     "client_uid": <CLIENT_UID>,
     "event_id": "<EVENT_ID>",
     "encryption_key": "<CHANNEL_KEY>",
     "encryption_kdf_salt": "<KDF_SALT_B64>",
     "encryption_mode": "AES_256_GCM2"
   },
   "shengwang_device": {"uid": <PUBLISHER_UID>}
 }}
```

| Field (under `data`) | Type | Meaning | Required by 2.x |
|---|---|---|---|
| `shengwang` | object or absent | The credentials block. **Absent = no credentials** (see §1.5). | yes (its absence is the failure signal) |
| `shengwang.appid` | string | Agora application id to join with. Anycubic's own (expected to be the same for every printer, not verified across accounts); treat as a credential and never log it. | yes |
| `shengwang.channel` | string | Agora channel name. Observed equal to the printer key. | yes |
| `shengwang.rtc_token` | string | Agora join token ("AccessToken2", `007…` prefix). Bound to `client_uid`, signed by Anycubic's Agora app certificate — cannot be minted or extended by a client. Research decoding put its privilege lifetime at **3600 s**. | yes |
| `shengwang.client_uid` | integer | The uid **this viewer** joins as. **Rotates on every 1001 call** — credentials are single-use per viewing session and must never be cached or shared between sessions. | yes |
| `shengwang.uid` | integer | The printer's (publisher's) uid; observed stable across calls. | no — fallback for the publisher uid |
| `shengwang_device.uid` | integer, optional | Publisher uid as the slicer reads it. **Absent** on the maintainer's account; 2.x falls back to `shengwang.uid`. Only useful to recognise the publisher's join/leave events. | no |
| `shengwang.encryption_mode` | string | Agora enum spelling, `AES_256_GCM2`. The wire form is lower-case with `_`→`-` (`aes-256-gcm2`). Absent, empty or `none` means an unencrypted channel. | if present |
| `shengwang.encryption_key` | string | Channel secret, a **32-character string**. Used as its raw UTF-8 bytes — it looks like hex but must **not** be hex-decoded. | if encrypted |
| `shengwang.encryption_kdf_salt` | string | Base64 that decodes to exactly 32 bytes. Passed on unchanged (see §1.7). | if encrypted |
| `shengwang.event_id` | string | `<timestamp>-<printer key>-<hex>`; a per-session tracking id. Not used. | no |
| `msgid` | string | Order message id; kept, not used. | no |
| `token` | string | Present in the reply; purpose unknown; not used. | no |

**Expiry** is not a field of the reply; it lives inside `rtc_token` (see §1.6).
**Region**: the reply carries none. The Anycubic region (international/China)
only decides which cloud the 1001 POST goes to; the Agora region is learned
from Agora's own access-point reply (§1.7).

A legacy credential shape — `secret_id`, `secret_key`, `session_token`,
`region`, `msg_id` (a Tencent/AWS STS style token) — is still modelled in the
library but is dead: nothing produces or reads it. The slicer's helper for this
call is named after AWS, and a second viewer (AWS KVS) reportedly exists for
printers without `shengwang_rtc_support`; unverified.

#### 1.5 Failure modes and the retry

| Symptom | Cause | 2.x handling |
|---|---|---|
| `msg` "Video service upgraded. Update the slicer to enable.", no `shengwang` | `shengwang_rtc_support` missing | never happens in 2.x (flag always sent) |
| "Operation successful", `data` present, **no `shengwang` block**, while every other API call works | **Another Anycubic session holds the camera.** The cloud gives the camera to the account's *most recent* login — opening the slicer or phone app steals it. The token is still valid, so a normal "is my token OK" check does not refresh it. | Retry **once**: discard the cached user access token, perform a full fresh login (making this the most recent session again), resend the identical 1001, parse again. Verified by A/B on real hardware to recover. |
| Still no block after the retry | The printer genuinely has no camera, or the other session logged in again | Fail. 2.x raises a Home Assistant error saying either the printer has no camera or another Anycubic session (slicer or phone app) has taken the account, and to close it and retry. |
| `data` null | server-side failure | treated as "no credentials" (same path as above) |

If discarding the cached token or the fresh login fails, 2.x returns "no
credentials" without a second request.

#### 1.6 Session lifetime, renewal and stopping

- **Lifetime**: bounded by `rtc_token` (3600 s per research decoding). Agora
  sends `on_token_privilege_will_expire` then `on_token_privilege_did_expire`
  on the gateway socket.
- **Renewal**: not possible from the client — a new token needs a new 1001,
  which also yields a new `client_uid`, i.e. a new join. Anycubic's own client
  never calls Agora's token renewal; it caches credentials for **30 s**, re-joins
  on loss, and self-throttles (at most 3 reconnections, 3000 ms join cooldown).
  2.x only logs "will expire"; on "did expire" the stream ends and the viewer
  must start a new one (which triggers a fresh 1001).
- **Per viewing session**: 2.x issues a fresh 1001 for every WebRTC offer from
  the browser, never caching.
- **Stopping**: 2.x closes the Agora websocket (on the browser ending the
  session, or entity removal). It never sends order **1002** (`CAMERA_CLOSE`,
  defined but unused) and nothing else tells the printer to stop publishing.
  Whether the printer stops on its own when the channel empties is unknown.
- **Slicer telemetry** (not done by 2.x): after joining, the slicer publishes
  `{"type": "video", "action": "startCapture", "state": ..., "code": ...}` to
  `anycubic/anycubicCloud/v1/web/printer/<MACHINE_TYPE>/<PRINTER_KEY>/video/report`
  (note `web`, not `pc`). Research reads it as after-the-fact telemetry, not a
  trigger; 2.x streams fine without it.
- The slicer's own MQTT topic family `.../v1/pc/printer/...` is ACL-denied
  (`granted = 128`) to a third-party client; do not subscribe to it.

#### 1.7 What the Agora client needs, and where each piece comes from

The rewrite may use `Jezza34000/homeassistant_petkit` (MIT) as the Agora
client (DECISIONS V8). Architecture used by 2.x and recommended: **signalling
only**. The browser is the WebRTC peer; Home Assistant relays the browser's SDP
offer to Agora's edge gateway and returns the gateway's answer. No media, no
decoding, no AES in Home Assistant.

**Inputs to wire:**

| Agora client needs | Source |
|---|---|
| App id | 1001 reply `shengwang.appid` |
| Channel name | `shengwang.channel` |
| Join token (the only Agora credential; sent verbatim as the AP `key` and the join `channel_key`) | `shengwang.rtc_token` |
| Join uid (numeric) | `shengwang.client_uid` |
| Publisher uid (optional, for filtering join/leave events) | `shengwang_device.uid`, else `shengwang.uid` |
| Encryption mode (wire spelling) | `shengwang.encryption_mode`, lower-cased with `_`→`-` |
| Encryption secret | `shengwang.encryption_key` as raw UTF-8 (see deltas below) |
| Encryption salt | `shengwang.encryption_kdf_salt`, unchanged base64 |
| Browser offer SDP, session id, trickled ICE candidates | Home Assistant's WebRTC camera hooks (offer handler, candidate handler, close handler). 2.x uses HA's WebRTC session id as the Agora join `session_id`. |
| Answer SDP to return | built from the gateway's join reply (`ortc`) plus the publisher's announced video stream |
| Edge servers, per-edge ticket and DTLS fingerprints, Agora region hint | Agora access-point ("choose server") reply — fields `edges_services`, `cert`, `detail["19"]` (`;`-separated fingerprints, positionally matched to edges), `detail["23"]` (region redirect for later lookups) |
| HTTP session | Home Assistant's shared aiohttp session |

**Anycubic-specific values and deltas versus PetKit** (PetKit's channels are
unencrypted, so the encryption part has no counterpart there):

| Item | Value / rule |
|---|---|
| Client mode / codec / role | `live` / `h264` / role **`host`** (the slicer uses host although it only subscribes); after join, 2.x sends `set_client_role` host, level 0 |
| SDK version string claimed | `4.24.0` (the `agora-rtc-sdk-ng` build the slicer ships); a current desktop Chrome user agent |
| Access-point hop | one HTTPS POST to `/api/v2/transpond/webrtc?v=2` on `webrtc2-ap-web-1.agora.io` / `webrtc2-2.ap.sd-rtn.com` (raced), backups `webrtc2-ap-web-3.agora.io` / `webrtc2-4.ap.sd-rtn.com` after 1 s; body is `multipart/form-data` with one field named `request` holding JSON; area code the literal string `CN,GLOBAL` (detail keys 11 and 22), role detail 17 = `"1"` (host); service ids 11 (choose server) and 26 (proxy fallback); uri 22. Timeouts used: 10 s per request, 20 s overall. |
| Gateway URL | `wss://<edge ip with dots replaced by dashes>.edge.agora.io:<port>` (so the wildcard certificate validates); try each edge in turn |
| Join verb | `join_v3`, 15 s timeout; `ap_response` echoed with the dialled edge's ticket |
| **Encryption fields in the join message** | `aes_mode` = wire mode (`aes-256-gcm2`); `aes_secret` = base64 of the channel key's raw UTF-8 bytes encrypted with **RSA-OAEP (SHA-256, MGF1-SHA-256, no label)** under the 1024-bit RSA public key embedded in `agora-rtc-sdk-ng` 4.24.0 (see **Agora's key** below); `aes_encrypt` = `true` (marks the secret as wrapped — a fourth field that is easy to miss); `aes_salt` = the salt string exactly as received (validate it decodes to 32 bytes; do not re-encode). The SDK's internal names `aesmode`/`aespassword`/`aessalt` must **not** appear on the wire. Omit all four for an unencrypted channel. |
| **Agora's key** | Agora's own **public** key, published in its Web SDK: `agora-rtc-sdk-ng` 4.24.0, file `AgoraRTC_N-production.js`, where it is passed to `window.atob(...)` and imported as SPKI for RSA-OAEP. Verified by the specification team against the npm package on 2026-09-28. It is Agora's, it is public, and it is protocol data, not an Anycubic credential. The library ships it as the **default** and lets a caller override it. SPKI, DER, base64: `MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDCMnXAHkKIGAM+x4N22gCI+WyuSTM9ztkT3uYslTT2PuKmZfPzhH6kVdO7PTjGCOZnAsyb3oTtWat0KcxQ4jxvqQV+HvYl3iI1Yd4vl2c3qRMJPLtRDfNxa2Mcxgq7e9aEUibzdd0st+OJAy3tOj/Y0aVyxQiYDz3vqa6bP29adwIDAQAB` |
| Who decrypts | Agora's edge, not the viewer: the browser leg is ordinary DTLS-SRTP. Proven by the gateway owning error `2028 ILLEGAL_AES_PASSWORD` and pushing `on_crypt_error`. Hex-decoding the key yields 2028. |
| ICE | The gateway is ICE-lite: joining with **zero** browser candidates works (confirmed live). Candidates that arrive before the join may be added to the offer blob (2.x waits at most 0.3 s for one); candidates after the join must be **dropped** — the gateway has no verb to add one. |
| SSRC | The publisher's video SSRC must be in the **first** answer (HA's WebRTC channel cannot renegotiate). Subscribe to streams listed in the join reply and to `on_add_video_stream` (fields `uid`, `ssrcId`, optional `rtxSsrcId`, `cname`, `video`), then wait up to **8 s** for one before answering; if none arrives, answer without it (logged: the printer is probably not publishing yet). |
| Subscribe message | per stream: stream id = publisher uid, `stream_type` video, mode live, codec h264, `ssrcId`, twcc and rtx on |
| Keep-alive | ping every 3 s (the slicer's interval; the gateway drops idle sockets within a minute) |
| Server events worth handling | `on_add_video_stream`, `on_remove_video_stream`, `on_user_online`/`offline`, `on_crypt_error` (→ fail: key/salt wrong), `on_p2p_lost`, `error`, `on_token_privilege_will_expire`, `on_token_privilege_did_expire`; gateway error 2024 = rejoin token invalid |
| TURN credentials in the AP reply | not needed (the browser reaches the edge directly) |
| No signatures | nothing in the AP or gateway exchange is signed; the token is the only credential; origin is not enforced |

#### 1.8 How 2.x exposes it (entity level: BEHAVIOUR §2.18)

| Aspect | 2.x |
|---|---|
| Entities | Two per printer: `camera` (LAN HTTP-FLV) and `cloud_camera` (Agora). Separate because HA fixes a camera class's stream type once per class: defining the WebRTC offer handler flips the class to WebRTC and would drop the LAN camera's HLS path. |
| `cloud_camera` stream type | WebRTC (native async offer handler); feature flag STREAM; no `stream_source` |
| Still image | a fixed placeholder PNG shipped with the integration, read once and cached. No real still exists: Anycubic has no snapshot endpoint and the only frames exist in the browser. Dashboards must use a live view; the default "auto" view otherwise shows the placeholder. |
| Snapshots, entity picture, recording | not possible on this entity |
| Availability | printer loaded from the cloud **and** the peripherals reply has not said `camera` is false (unknown counts as available) |
| Sessions | one Agora session per HA WebRTC session id; all closed on entity removal; close is scheduled without blocking |
| Errors | Agora failures surface as a Home Assistant error "Could not open the Anycubic cloud camera: <reason>"; credential failures as in §1.5 |
| Transport | cloud only; on a LAN connection the cloud does not know the printer, so 1001 cannot succeed (QUESTIONS.md: 3.0 does not create it on LAN entries) |

#### 1.9 Limitations; printers without a camera

- **One viewer account at a time**: whoever logged in most recently owns the
  camera; the slicer or phone app will take it from Home Assistant and vice
  versa (each 2.x retry performs a fresh login, which in turn kicks the other
  client).
- No stills, no recording, and a hard session end at token expiry.
- Policy risk: if Anycubic rate-limits 1001, failures appear as a missing
  credential block or a 4xx from Anycubic, not as a WebRTC error.
- **Printer without a camera**: after the peripherals reply says `camera: 0`,
  the entity is unavailable. Before that reply, opening the stream runs 1001,
  gets no credentials (after the re-login retry) and fails with the
  "no camera or another session" message.

---

### 2. Printing

#### 2.1 The start-print order (order 1) — envelope

All three starting routes use `POST /work/operation/sendOrder` with the
**project-level** shape (§0.2; integer `order_id` 1, `project_id` 0):

```json
{"order_id": 1, "printer_id": <PRINTER_ID>, "project_id": 0,
 "data": { ...see 2.2 / 2.3... },
 "ams_info": {"use_ams": true, "ams_box_mapping": [ ...see 2.4... ]},
 "settings": null}
```

| Key | Type | Value in 2.x |
|---|---|---|
| `order_id` | integer | 1 |
| `printer_id` | integer | target printer |
| `project_id` | integer | always 0 (no job exists yet) |
| `data` | object | the print request (§2.2, §2.3) |
| `ams_info` | object or null | null unless an ACE mapping is sent; with a mapping: `use_ams` = true whenever the list is non-empty, `ams_box_mapping` = list (§2.4). An empty mapping list is sent as `ams_info: null`. |
| `settings` | null | always null — no print-settings overrides are ever sent at start |

The order has **no LAN form** in 2.x (it goes to the cloud even when a LAN link
exists, and the cloud cannot reach a printer in LAN Mode).

#### 2.2 `data` common part (every route)

| Field | Type | 2.x value | Notes |
|---|---|---|---|
| `filetype` | int | 0 cloud file, 1 printer internal storage, 2 USB stick | set by route |
| `file_key` | string | `""` | never filled |
| `file_name` | string | `""` | never filled (the local route uses `filename` instead) |
| `task_settings.ai_detect` | int | **0** | AI failure detection for this job; 2.x offers no way to turn it on |
| `task_settings.camera_timelapse` | int | **0** | timelapse for this job; 2.x offers no way to turn it on |

No other print-time options (auto-leveling, vibration compensation, flow
calibration, "dry first", preheat, etc.) are sent by 2.x. The cloud's printer
record advertises matching capabilities (`auto_leveling_support`,
`vibration_compensation_support`, `flow_calibration_support`,
`drying_first_support`, `camera_timelapse_support`, `preheating_support`), but
their start-print field names are **not** known from any source (open point).

#### 2.3 `data` route-specific part

**Cloud file** (`filetype` 0):

| Field | Type | 2.x value | Notes |
|---|---|---|---|
| `file_id` | int | the cloud file id | from upload claim (§2.5) or from `gcode/infoFdm` `file_id` |
| `is_delete_file` | int | 1 for a temporary upload, else 0 | asks the cloud to delete the file after printing |
| `project_type` | int | 1 | |
| `template_id` | int | 0 | |
| `matrix` | string | `""` | |
| `hollow_param`, `punching_param`, `slice_param`, `slice_size` | null | null | resin/slicing parameters, never used |

**Printer-held file** (`filetype` 1 internal storage; 2 USB):

| Field | Type | 2.x value | Notes |
|---|---|---|---|
| `filename` | string | exact name as listed by the printer (§3.3) | |
| `filepath` | string | `"/"` followed by the path; 2.x's path is empty, so always `"/"` | files in sub-folders cannot be addressed by 2.x |

2.x exposes only `filetype` 1 (action `print_local_file`, BEHAVIOUR §4.4). The
USB variant exists in the library (identical, `filetype` 2) but no action uses
it. No `ams_info` is sent for printer-held files, even on an ACE printer — how
the printer then chooses slots is unknown.

```json
{"order_id": 1, "printer_id": <PRINTER_ID>, "project_id": 0,
 "data": {"filetype": 1, "file_key": "", "file_name": "",
          "task_settings": {"ai_detect": 0, "camera_timelapse": 0},
          "filename": "part.gcode", "filepath": "/"},
 "ams_info": null, "settings": null}
```

#### 2.4 ACE slot mapping (`ams_info.ams_box_mapping`)

Inputs: the file's **colour list** (one entry per colour/"paint" the file uses,
§2.6/§2.7) and the user's **slot list** (one ACE slot per colour, in the
file's colour order; the action takes them 1-based, 2.x converts to 0-based
global indices: 0–3 first ACE, 4–7 second).

Rules 2.x applies (BEHAVIOUR §4.4 has the user-facing form):

1. Printer with an ACE → a slot list is required; without an ACE → it must be
   absent. Violations are errors before anything is uploaded.
2. Slot list length must equal the colour-list length.
3. Highest global slot index ÷ 4 (integer) + 1 must not exceed the number of
   ACE units reported; else error "not enough ACE units".
4. For each ACE unit in reported order, for each colour *x*: global index *g* =
   slot list[*x*]; slot within the box = *g* − 4 × (that box's reported `id`);
   entries whose in-box slot falls outside 0–3 are skipped for that box.
5. The combined list is sorted by `paint_index`.

Entry fields:

| Field | Type | Source |
|---|---|---|
| `ams_index` | int | the global 0-based slot index *g* |
| `paint_index` | int | colour's `paint_index` from the file/cloud colour list |
| `material_type` | string | colour's `material_type` from the file/cloud colour list (e.g. `PLA`) |
| `filament_used` | float | colour's planned grams (`filament_used`) |
| `ams_color` | [r, g, b] ints | the **slot's current colour** as last reported by the ACE |
| `paint_color` | [r, g, b] ints | **also the slot's colour** in 2.x (not the file's paint colour) |

```json
"ams_info": {"use_ams": true, "ams_box_mapping": [
  {"ams_color": [255, 255, 255], "ams_index": 0, "filament_used": 12.34,
   "material_type": "PLA", "paint_color": [255, 255, 255], "paint_index": 0},
  {"ams_color": [0, 0, 0], "ams_index": 2, "filament_used": 3.21,
   "material_type": "PLA", "paint_color": [0, 0, 0], "paint_index": 1}]}
```

#### 2.5 Route (c): upload and print

##### 2.5.1 Uploading a file to Anycubic storage

| Step | Request | Parameters | Reply used | Failure |
|---|---|---|---|---|
| 1 | — | read the bytes; size must be > 0 | — | empty file error |
| 2 (permanent uploads only) | `POST /work/index/getUserStore` (no body) | — | `data.total_bytes − data.used_bytes` must be ≥ file size (§3.5) | "no space" error |
| 3 | `POST /v2/cloud_storage/lockStorageSpace` | `size` (int bytes), `name` (the uploaded file name), `is_temp_file` (1 temporary, 0 permanent) | `data.id` (the lock id), `data.preSignUrl` | — |
| 4 | `PUT <PRESIGNED_PUT_URL>` | the raw file bytes as the body; no Anycubic auth headers | any **non-empty** response body = failure | upload error |
| 5 | `POST /v2/profile/newUploadFile` | `user_lock_space_id`: the lock id | `data.id` = **cloud file id** | missing `data.id` = "claim failed" |
| 6 | `POST /v2/cloud_storage/unlockStorageSpace` | `id`: lock id, `is_delete_cos`: 1 if the upload failed, else 0 | ignored | — |
| 7 (permanent uploads only) | `POST /work/index/getUserStore` | — | free space must have dropped by at least the file size | "file not found in cloud" error |

Returns the cloud file id. 2.x quirk: a failure in steps 4–5 raises **before**
step 6, so the unlock (and its `is_delete_cos: 1` clean-up) never runs on
failure; the lock is left to the server.

##### 2.5.2 Save in cloud (`print_and_upload_save_in_cloud`)

1. Validate the ACE/slot preconditions (§2.4 rule 1).
2. Upload permanently (§2.5.1) → cloud file id.
3. Fetch the newest cloud file: `POST /work/index/files` with
   `{"page": 1, "limit": 10, "printable": 1, "machine_type": 0}`; the first
   entry must have `id` equal to the new cloud file id (else "upload mismatch"
   error) and a non-null `gcode_id` (else error).
4. `GET /work/gcode/infoFdm?id=<GCODE_ID>` (§2.7) → the cloud's parse of the
   file, including the colour list `slice_param.paint_infos`.
5. If a slot list was given: colour list must be non-empty and match the slot
   list's length; build the mapping (§2.4).
6. Start the print (§2.1) with `file_id` = the `file_id` from step 4,
   `is_delete_file` 0, mapping from step 5 — with the retry of §2.5.4.
7. Result: message id, printer id, `saved_in_cloud: true`, file name, cloud
   file id, `gcode_id`, colour list, mapping — the data of the
   `anycubic_cloud` / `print_cloud_start` event (BEHAVIOUR §4.8).

##### 2.5.3 No cloud save (`print_and_upload_no_cloud_save`)

1. Validate the ACE/slot preconditions.
2. If a slot list was given: the file name must end `.gcode`; read the colour
   list from the file's own header (§2.6); validate and map as above. Without a
   slot list the file is not parsed at all.
3. Upload as a **temporary** file (`is_temp_file` 1; the quota checks of steps
   2 and 7 are skipped).
4. Start the print with the returned cloud file id, `is_delete_file` 1, and the
   mapping.
5. Result as above with `saved_in_cloud: false`, `gcode_id` null.

##### 2.5.4 Start retry

The start order is attempted up to **3 times**. Only a "file not found in the
cloud" reply (§0.2: null `data` with `msg` `No file found`) is retried, after
a 3 s sleep; any other error propagates at once. 2.x quirk: if all three
attempts hit "not found", the call returns no message id **without raising**,
so the action still reports success and fires the event with `order_msg_id`
`"None"`.

Neither print-and-upload action wakes CMQTT; `print_local_file` does not either.

#### 2.6 Reading gcode metadata from an uploaded file (no-cloud-save path only)

- The file is decoded as UTF-8 text and split into lines. Decoding failure is
  an error.
- Lines are ignored until the first line beginning with `; filament used`;
  from there to the end of the file, every line of the form
  `; <key> = <value>` is collected (key characters: letters, digits,
  underscore, square brackets, spaces).
- Key normalisation: spaces, `[`, `]`, `(`, `)` become `_`, then `__` becomes
  `_`, and one trailing `_` is removed — so `; filament used [g] = …` becomes
  `filament_used_g`.
- Value: parsed as JSON if possible; otherwise, if it contains commas, a list
  of comma-separated items, each turned into an int, else a float, else kept as
  text; otherwise a single int/float/text. The values `begin` and `end` are
  skipped.

Keys used:

| Header line (as written by the slicer) | Normalised key | Content | Use |
|---|---|---|---|
| `; paint_info = [...]` | `paint_info` | JSON list, one object per colour; each has at least `paint_index` (int) and `material_type` (string); all its other keys are carried through untouched | the colour list |
| `; filament used [g] = a, b, …` | `filament_used_g` | grams per filament, indexed by `paint_index` | `filament_used` of each colour |
| `; filament used [mm] = …` | `filament_used_mm` | mm per filament | carried as `filament_used_mm` (null if the list is shorter than the colour list) |
| `; filament used [cm3] = …` | `filament_used_cm3` | cm³ per filament | carried as `filament_used_cm3` (same rule) |

Errors: no `paint_info` → "empty paint info"; missing/empty
`filament_used_g` → error; `filament_used_g` shorter than `paint_info` →
error. Each colour's grams are looked up **by its `paint_index`**, not by its
position. The colour list becomes the event's `material_list`.

#### 2.7 `GET /work/gcode/infoFdm?id=<GCODE_ID>` reply (`data`)

| Field | Type | Use in 2.x |
|---|---|---|
| `file_id` | int | the cloud file id to print (becomes the order's `file_id`) |
| `gcode_id` | int | echoed |
| `name` | string | file name |
| `size` | int | bytes |
| `create_time` | int | epoch seconds |
| `estimate` | int | estimated print time |
| `status`, `progress`, `machine_class` | int | not used |
| `image_id` | string | preview image path (see §5.6) |
| `slice_result` | JSON string or object | not used here |
| `slice_param` | JSON string or object | `paint_infos`: list of `{paint_index, filament_used, material_type, …}` — the cloud colour list |

#### 2.8 Route (a): printing a file already in the cloud

The library can print any stored cloud file by `file_id` (§2.3, `filetype` 0,
`is_delete_file` 0) or by `gcode_id` (steps 4–6 of §2.5.2), with the same
mapping rules. **2.x exposes neither as an action**; its only cloud-file print
is the tail of save-in-cloud. The file-list sensor gives `id` but not
`gcode_id` (§3.2), so a 3.0 "print cloud file" action would need to keep
`gcode_id` or call infoFdm some other way.

#### 2.9 What the printer reports back

- HTTP: the order reply's `data.msgid` (kept as the event's `order_msg_id`).
- CMQTT `type: "print"` reports, each carrying `data.taskid` (the new job id):

| `action` | `state` | Meaning / data |
|---|---|---|
| `start` | `downloading` | printer fetching the file; `data.progress` = download % |
| `start` | `checking` | file check |
| `start` | `preheating` | heating; job fields as in printing |
| `start` | `printing` | printing; `curr_layer`, `total_layers`, `filename`, `print_time`, `progress`, `remain_time`, `supplies_usage` |
| `start` or `update` | `updated` | live settings/temperatures (`settings`, `curr_*_temp`) |
| `start` | `finished` | done |
| `start` or `stop` | `stoped` (sic) or `stopping` | cancelled |
| `start` or `stop` | `failed` | start refused/failed; reason in envelope `msg` |
| `pause` / `resume` | `pausing`, `paused` / `resuming`, `resumed` | |
| `getSliceParam` | `done` | `data.slice_param` |

Job modelling from these reports belongs to the job spec (BEHAVIOUR §1.3).
2.x also schedules a forced refresh **5 s** after CMQTT shows a printer going
from available to busy, so the new job is picked up from the cloud project
list.

#### 2.10 Defaults 2.x fills, in one place

| Item | 2.x value |
|---|---|
| `task_settings.ai_detect` / `camera_timelapse` | 0 / 0, always |
| `settings` | null |
| `project_id` | 0 |
| `project_type`, `template_id`, `matrix` | 1, 0, `""` |
| `filepath` (printer-held file) | `"/"` |
| `is_delete_file` | 1 for no-cloud-save, else 0 |
| Upload read retries (HA upload not yet available) | 3 attempts, 1 s apart (BEHAVIOUR §4.4) |
| Start retries | 3 attempts, 3 s after each "No file found" |

---

### 3. File management

#### 3.1 Summary

| Source | List | Delete | Transport | Reply |
|---|---|---|---|---|
| Account cloud storage | `POST /work/index/files` | `POST /work/index/delFiles` | HTTP only | in the HTTP reply |
| Printer internal storage ("local") | order **103** | order **104** | order over HTTP | **CMQTT only** (`file`/`listLocal`, `file`/`deleteLocal`) |
| USB stick ("udisk") | order **101** | order **102** | order over HTTP | **CMQTT only** (`file`/`listUdisk`, `file`/`deleteUdisk`) |

None of 101–104 has a LAN form in 2.x (LAN has an unrelated `file`/`fileDetails`
command, PROTOCOL/research only). Lists are held in memory only.

#### 3.2 Cloud file list

Request body (all optional except as noted):

| Field | Type | 2.x value | Meaning |
|---|---|---|---|
| `page` | int | 1 | 1-based page |
| `limit` | int | 10 | page size |
| `printable` | int | omitted for the sensor; 1 for "newest printable" | filter to printable files |
| `machine_type` | int | omitted for the sensor; 0 for "newest printable" | model filter (0 meaning unknown — "any"?) |

Reply `data` is a **list** of file objects (no total or page count is read;
2.x fetches only page 1, i.e. the 10 most recent). Null or empty list → no
files. Any null entry → parse error. Fields present (types as modelled; only
the "2.x uses" column is load-bearing):

| Field | 2.x uses as | Notes |
|---|---|---|
| `id` | `id` | cloud file id (delete, print) |
| `old_filename` | `name` | the user's original name |
| `filename` | — | storage name |
| `size` | `size_mb` = size ÷ 1,000,000 | bytes |
| `thumbnail` | `thumbnail` | URL; empty → null |
| `estimate` | `estimate_seconds` | seconds |
| `material_name` | `material` | empty → null |
| `layer_height` | `layer_height` | mm |
| `supplies_usage` | `filament_mm` | mm of filament |
| `size_x`, `size_y`, `size_z` | `dimensions` `{x, y, z}` (null when `size_x` is null) | mm |
| `gcode_id` | — (only in the upload path) | needed to print by gcode id |
| `user_id`, `post_id`, `time`, `status`, `ip`, `img_status`, `device_type`, `file_type`, `md5`, `url`, `is_delete`, `update_time`, `uuid`, `store_type`, `bucket`, `region`, `path`, `thumbnail_nonce`, `sliceparse_nonce`, `file_extension`, `name_counts`, `source_user_upload_id`, `origin_post_id`, `stl_user_upload_id`, `is_official_slice`, `triangles_count`, `is_parse`, `source_type`, `file_source`, `user_lock_space_id`, `origin_file_md5`, `is_temp_file`, `official_file_key`, `official_file_id`, `simplify_model`, `printer_names`, `slice_param` | — | read, not used |

```json
{"code": 1, "msg": "...", "data": [
  {"id": <CLOUD_FILE_ID>, "gcode_id": <GCODE_ID>, "old_filename": "benchy.gcode",
   "filename": "<storage name>", "size": 1234567, "thumbnail": "<URL>",
   "estimate": 3600, "material_name": "PLA", "layer_height": 0.2,
   "supplies_usage": 4567, "size_x": 60.0, "size_y": 31.0, "size_z": 48.0,
   "is_temp_file": 0, "url": "<URL>"}]}
```
(Shape assembled from the fields 2.x reads; not a verbatim capture.)

The list is account-wide: every printer shows the same list.

#### 3.3 Local and USB file lists

Request (project-level shape, empty payload):

```json
{"order_id": 103, "printer_id": <PRINTER_ID>, "project_id": 0, "data": {}}
```
(101 for USB.) Reply over CMQTT:

```json
{"type": "file", "action": "listLocal", "state": "done", "code": 200,
 "msg": "done", "msgid": "<MSGID>",
 "data": {"records": [
   {"filename": "part.gcode", "timestamp": 1760000000, "size": 2345678, "is_dir": false}]}}
```
(`listUdisk` for USB; record shape from 2.x's parser, not a verbatim capture.)

| Record field | Required | 2.x use |
|---|---|---|
| `filename` | yes | `name` |
| `is_dir` | yes | kept, not filtered — directories appear in the list |
| `size` | no (0) | `size_mb` = size ÷ 1,000,000 |
| `timestamp` | no (0) | kept, not shown |

No pagination is known. A null `data.records` leaves the previous list; an
empty list replaces it (and then reads as "no value", BEHAVIOUR G12). A record
that cannot be parsed fails the whole list.

#### 3.4 Deletes

| Target | Request | Reply | 2.x follow-up |
|---|---|---|---|
| Local | order 104, project-level, `data: {"filename": <name>, "filetype": -1, "path": "/"}` | CMQTT `file`/`deleteLocal`/`success` (ignored) | request the local list 2 s later and again 5 s after that |
| USB | order 102, same payload | CMQTT `file`/`deleteUdisk`/`success` (ignored) | same with the USB list |
| Cloud | `POST /work/index/delFiles` `{"idArr": [<CLOUD_FILE_ID>]}` | success **only if `data` is the empty string** | 5 s later re-fetch the cloud list; failure → error "Failed to delete cloud file." |

`delete_batch_support` in the printer's features suggests the printer accepts
several names at once; 2.x deletes one at a time. `idArr` is already a list.

#### 3.5 Cloud storage quota

`POST /work/index/getUserStore` (no body) →
`data: {"used_bytes": int, "total_bytes": int, "used": "<text>", "total": "<text>", "user_file_exists": bool}`.
Available = `total_bytes − used_bytes`. 2.x uses it only inside a permanent
upload (§2.5.1 steps 2 and 7); it is not exposed as an entity.

#### 3.6 The `request_file_list_<source>` buttons and `file_list_<source>` sensors

| Entity | Data source | Notes |
|---|---|---|
| `request_file_list_local` | wakes CMQTT, sends 103; 5 s later, if neither the previous nor the new list exists and the printer is online, restarts CMQTT, waits for it, waits 2 s and sends 103 again (single-flight: a second press while this check runs does not start another) | ends with a forced refresh |
| `request_file_list_udisk` | wakes CMQTT, sends 101 | no retry |
| `request_file_list_cloud` | wakes CMQTT (unnecessarily — the list is HTTP), then §3.2 page 1, limit 10, no filters | |
| `file_list_local` / `file_list_udisk` | the last `listLocal` / `listUdisk` records | value = count, attribute `file_info` = `[{name, size_mb}]` |
| `file_list_cloud` | the last cloud list | attribute `file_info` = `[{id, name, size_mb, thumbnail, estimate_seconds, material, layer_height, filament_mm, dimensions}]` |

Presentation, "no value" rules and translation keys: BEHAVIOUR §2.5, §2.12, §4.5.
The panel uses these same actions and buttons (and HA's standard file upload
for gcode); it has no backend endpoints of its own.

#### 3.7 Other file messages

- CMQTT `file`/`cloudRecommendList`/`done`: consumed and discarded (content
  unknown).
- On LAN only, the printer's `info` report names a signed direct-upload URL
  (`urls.fileUploadurl`, `http://<printer>:18910/gcode_upload?s=<signed>`) —
  PROTOCOL §6.1. 2.x stores it but never uploads to it.

---

### 4. Firmware

#### 4.1 Installed and latest versions

**Cloud printer record** — `GET /v2/printer/info?id=<PRINTER_ID>` (each cloud
refresh) and `GET /work/printer/getPrinters` (list) carry a `version` object;
the info call also carries `multi_color_box_version` (one entry per ACE):

| Field | Type | Meaning | 2.x use |
|---|---|---|---|
| `firmware_version` | string | installed version | installed version |
| `target_version` | string | version the cloud offers; **observed equal to the installed version when nothing is available** (Kobra S1 2.7.2.7 and ACE Pro 1.3.863 fixture, `need_update` 0) — answers BEHAVIOUR V17 | latest version |
| `need_update` | int 0/1 | 1 = an update is offered | gate for install |
| `update_progress` | int | progress as the cloud sees it | copied into the update progress when non-null |
| `update_status` | string (often `""`) | | kept |
| `update_desc` | string | release notes text | kept, not shown |
| `force_update` | int | forced-update flag | kept, not acted on |
| `update_date` | int | | kept |
| `time_cost` | int | unknown unit (10 on the S1 fixture; perhaps expected minutes) | kept |
| `img` | string | picture URL | not read |
| `box_id` (ACE entries) | int | ACE index | kept |
| `box_name` (ACE entries) | string | e.g. `ACE Pro` | not read |

The printer record also has a top-level `need_update` (not read). The ACE list
is matched to units by position (entry 0 = first ACE, entry 1 = second).
Each refresh overwrites installed/latest/`need_update` from the cloud.

```json
"version": {"need_update": 0, "firmware_version": "2.7.2.7", "update_progress": 0,
            "update_date": 0, "update_status": "", "update_desc": "<release notes>",
            "force_update": 0, "target_version": "2.7.2.7", "time_cost": 10, "img": "<URL>"},
"multi_color_box_version": [
  {"box_name": "ACE Pro", "need_update": 0, "firmware_version": "1.3.863",
   "update_desc": "", "force_update": 0, "target_version": "1.3.863", "time_cost": 0,
   "update_progress": 0, "update_date": 0, "update_status": "", "img": "<URL>", "box_id": 0}]
```

**CMQTT** `type: "ota"`, `action: "reportVersion"`, `state: "done"`,
`data.firmware_version` (also carries `device_unionid`, `machine_version`,
`peripheral_version`, `model_id` — read, unused). Printer vs ACE is decided by
the **topic**: if it contains `multiColorBox`, it is an ACE message and the box
id is the topic's 10th segment (0-based index 9; 0 if absent or not a number).

**LAN**: no firmware service; the printer's `info.version` is the installed
version (BEHAVIOUR G10).

#### 4.2 Triggering an update

| Target | Precondition in 2.x | Request | Success criterion |
|---|---|---|---|
| Printer | printer has a version record **and** `need_update` = 1; otherwise nothing is sent and nothing is reported | `GET /work/printer/update_version?id=<PRINTER_ID>&target_version=<INSTALLED version>` — note 2.x passes the **installed** version under the name `target_version` (both as strings) | `data.update_status` = 1 → returns the cached `target_version` as the version being installed; anything else → no result, no error |
| ACE *n* (0 first, 1 second) | printer has an ACE, the ACE version list has entry *n*, and its `need_update` = 1 | `POST /v2/printer/update_multi_color_box_version` `{"id": <PRINTER_ID> (int), "box_id": n (int)}` | `data.target_version` equals the cached `target_version` → returned; otherwise no result, no error |

The library can also update every ACE in turn (not used by 2.x). The update
entity's install wakes CMQTT first (so progress can arrive), then triggers,
then forces a refresh. Cloud only.

#### 4.3 Progress messages (CMQTT `type: "ota"`)

| `action` | `state` | `data` | Effect |
|---|---|---|---|
| `update` | `start` | — | mark updating |
| `update` | `downloading` | `progress` (0–100) | mark updating + downloading; store download % |
| `update` | `updating` | `current_progress` (0–100) | mark updating, not downloading; store install % |
| `update` | `update-success` or `updateSuccessProcessed` | — | **ACE only**, ignored; for the printer these states are unknown messages (debug-logged, discarded) |
| `reportVersion` | `done` | `firmware_version` | if different from the installed version: install it, clear updating/downloading, zero both progress figures, clear `need_update` |

Progress figure and the `in_progress` rule: BEHAVIOUR §2.17 (0–50 % download
phase, 50–100 % install phase, 1 when started with no figures). The update
entity only ever reports a true/false `in_progress`.

```json
{"type": "ota", "action": "update", "state": "downloading", "code": 200,
 "msgid": "<MSGID>", "data": {"progress": 42}}
```

#### 4.4 Failure and timeout handling in 2.x

- **None.** There is no timeout, no failure state and no error surfaced after
  the trigger request.
- A trigger whose precondition fails, or whose reply does not match, does
  nothing silently.
- **Stuck-progress risk**: the "updating" flags are only cleared by a CMQTT
  `reportVersion` carrying a version *different* from the installed one. But
  every cloud refresh writes the installed version straight from the cloud
  record. If the refresh sees the new version first, the later `reportVersion`
  matches and clears nothing — `in_progress` stays true until restart. The
  same happens if CMQTT is down when the update finishes. 3.0 should end
  "in progress" whenever the installed version changes, from either source,
  and on a timeout.
- Messages for an ACE index with no version entry are ignored.

---

### 5. Other cloud-only features

#### 5.1 AI failure detection (entity: BEHAVIOUR §2.15)

- Set: order **1243**, printer-level with payload (string `order_id`, no
  `project_id`), `data: {"ai_settings": {"status": 3|0, "type": …, "count": …, "sensitivity_level": […], "notice_type": […]}}`;
  every field except `status` is copied from the printer's last report, with
  defaults `type` 2, `count` 60, `sensitivity_level` [1, 1], `notice_type`
  [0, 1]. Cloud only by Anycubic's design (the slicer marks it wide-area only).
- Report: `type: "aiSettings"` (action `query` seen on LAN), `data.ai_settings`
  = the same five keys; `status` non-zero = on. Over the cloud it arrives only
  when the printer volunteers it (e.g. after a change).
- Field meanings beyond `status` are unknown (see Open points).
- Per-job override exists as `task_settings.ai_detect` in the start order
  (§2.2), always 0 in 2.x.
- Capability hints: the printer's `features` has `fod_support`
  (foreign-object detection); the cloud function id 36 (AI detection) is
  **absent** from a Kobra S1 that does support 1243, so function ids are not a
  reliable gate for this feature.

```json
{"order_id": "1243", "printer_id": <PRINTER_ID>,
 "data": {"ai_settings": {"status": 3, "type": 2, "count": 60,
                          "sensitivity_level": [1, 1], "notice_type": [0, 1]}}}
```

#### 5.2 Timelapse

Only the per-job flag `task_settings.camera_timelapse` (§2.2, always 0 in
2.x). Capability hints: `camera_timelapse_support` feature, function id 39.
No endpoint for listing or downloading timelapse videos is known from any
source.

#### 5.3 Printer events and notifications

- CMQTT `type` `event`, `printerevent` or `printer_event`: consumed so they are
  not lost; nested `data` may carry `code`, `msg`, `msgid`, `state`, `action`;
  real shape not yet captured (a Kobra X filament run-out travels this way).
- Every CMQTT message's envelope `code` is inspected before dispatch; a code
  outside the "OK" set is kept as the printer's latest error code and message
  (error-code tables belong to the telemetry spec).
- Per-user topics
  `anycubic/anycubicCloud/v1/server/app/<USER_ID>/<md5 of user id>/slice/report`
  and `.../fdmslice/report` are subscribed; messages are only logged
  (cloud-slicing progress, content unknown).
- No push-notification or message-centre endpoint is used.

#### 5.4 Printer rename (library only)

`POST /work/printer/Info` `{"id": "<PRINTER_ID>" (string), "name": "<new name>" (string)}`
→ `data.name` equal to the new name = success; equal to the old name = "change
reverted" error; anything else = "unknown" error. Not exposed by 2.x. (LAN
equivalent `info`/`setPrinterName`, research only.)

#### 5.5 Unused / work-in-progress endpoints and orders

| Item | What is known |
|---|---|
| `GET /v2/project/printHistory` | print history; only logged by the library |
| `GET /v2/project/monitor?id=<job id>` | per-job monitor; only logged |
| `GET /v2/Printer/status?id=`, `/v2/printer/tool`, `/v2/printer/functions`, `/work/printer/printersStatus`, `/v2/printer/all` | printer metadata; outside this topic |
| Order 1002 `CAMERA_CLOSE` | defined, never sent (§1.6) |
| Order 44 `STOP_PRINT_FORCE`, 11 `IGNORE`, 12 `DETECT`, 1209 feed finish, 1210 refresh slot, 1215 filament control | defined, not handled |

#### 5.6 Job preview image (pointer)

The `job_image_url` image entity shows the current job's preview: a full URL
from the project if it has one, else the region's project-image base (an S3
bucket URL) followed by `slice_param.image_id`. Belongs to the job spec; noted
here because `infoFdm` and the cloud file list carry the same `image_id` /
`thumbnail` data.

---

### Open points

1. **Print-time options.** Field names and placement for auto-leveling,
   vibration compensation, flow calibration, dry-first and timelapse/AI per
   job, as the slicer sends them in order 1, are unknown; 2.x sends only
   `task_settings.{ai_detect, camera_timelapse}` = 0. Needs a slicer capture
   of a print start with those toggles set.
2. **`order_id` type for project-level orders.** 2.x sends orders 1 and
   101–104 with an integer `order_id` and `project_id` 0 and they are shipped
   features, but the slicer's own shape for them was never captured, and the
   same class of mistake silently disabled 201, 1231 and 1232. Capture before
   relying on it.
3. **ACE box id offset.** The mapping subtracts 4 × the box's *reported* `id`.
   The Kobra S1 fixture's cloud `multi_color_box` block shows `id: 1` for a
   single ACE; if real, every first-ACE slot would fall outside 0–3, the
   mapping would be empty and the print would start with `ams_info: null`.
   Verify which id the first ACE reports on each transport; 3.0 should offset
   by the box's position (0, 1), not its reported id, unless proven otherwise.
4. **`paint_color`.** 2.x sends the slot's colour as both `ams_color` and
   `paint_color`. Whether the slicer sends the file's paint colour in
   `paint_color`, and whether the printer cares, is unknown.
5. **Printer-held files on an ACE printer.** No `ams_info` is sent; unknown how
   the printer assigns slots. USB printing (`filetype` 2) and sub-folder paths
   are untested.
6. **Camera stop.** Whether the printer stops publishing when the viewer
   leaves, whether order 1002 (or the `video/report` telemetry) is needed, and
   how often 1001 may be called before Anycubic throttles it.
7. **Token lifetime.** 3600 s comes from decoding one token during research;
   confirm, and decide whether 3.0 re-issues 1001 and re-joins before expiry
   (the slicer re-joins; HA's WebRTC channel cannot renegotiate, so a re-join
   would need a new browser session).
8. **`shengwang_device`, `token`, `event_id` in the 1001 reply** — purpose
   unconfirmed; `shengwang_rdt_support` meaning unknown. The alleged AWS KVS
   viewer for printers without `shengwang_rtc_support` is unverified.
9. **Cloud file list**: meaning of `machine_type: 0` and `printable`, whether
   the reply has a total/page count, and whether page size can exceed 10.
   Local/USB `listLocal`/`listUdisk` replies: whether `data` has keys other
   than `records`, and whether the printer paginates large folders.
10. **Firmware**: the exact ACE `ota` topic layout (only "contains
    `multiColorBox`, box id at segment index 9" is known); whether the printer
    sends `update-success` or a failure state for itself; the meaning of
    `force_update`, `time_cost` and `update_status`; whether `target_version`
    on `update_version` should really be the installed version (as 2.x sends)
    or the offered one.
11. **Upload lock hygiene**: 2.x never unlocks a failed upload's storage lock
    (§2.5.1); whether the server expires it, and whether `is_delete_cos: 1`
    is needed to avoid consuming quota.
12. **AI settings semantics**: meaning of `type`, `count`, and the two-element
    `sensitivity_level` / `notice_type` arrays (likely one entry per detector,
    e.g. foreign object and first layer — unconfirmed).
13. **Event and `slice/report` payloads** are uncaptured.
14. **Retry that "succeeds"**: after three "No file found" replies 2.x reports
    success with `order_msg_id` "None" (§2.5.4) — 3.0 should fail instead.
