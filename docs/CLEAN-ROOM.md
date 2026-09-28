# Clean-room record

This library is written so that it contains **no code or expression** from
these GPL-3.0 projects:

- `WaresWichall/hass-anycubic_cloud` (the original Anycubic Cloud integration)
- `Nino6689/hass-anycubic` and `Nino6689/anycubic-cloud-api` (its fork and the
  fork's API library, published on PyPI as `anycubic-cloud-api`)

The fork's maintainer (Nino Bondonno) has worked on those projects, so the
work is split into two teams.

## Roles

| Role | Who | May read | Must not read |
|---|---|---|---|
| **Specification team** ("dirty") | Nino Bondonno, and Claude sessions on his machine that have read the GPL projects | Everything | — |
| **Implementation team** ("clean") | A separate Claude cloud session per phase, with no access to Nino's machine | This repository's `docs/`; `Nino6689/anycubic-lan` and `Nino6689/hass-anycubic-next` (both MIT); Python and dependency documentation; the MIT project `Jezza34000/homeassistant_petkit` for its Agora client | The three repositories above, the **source** of the PyPI package `anycubic-cloud-api`, any 0.x release of `anycubic-cloud-frontend`, and any other Anycubic integration or client |

Rules:

1. The specification team writes **facts and requirements only**: protocol
   behaviour, formulas, field names, captured payloads with personal data
   removed. It never writes or edits code in this repository.
2. The implementation team writes **all code**, from `docs/` and public
   documentation. When a fact is missing it asks in `docs/QUESTIONS.md`; it
   does not look elsewhere.
3. Feedback from live testing reaches the implementation team only as
   functional reports (observed behaviour against expected), never as code.
4. Before publication, the code is compared mechanically with the GPL
   projects and the result is recorded below.

## Anycubic's credentials

The cloud only answers clients that present Anycubic's own app credentials:
client ids, an app id, an app secret, and a TLS CA, client certificate and
client key for MQTT. These are Anycubic's data. **This library contains none of
them**, and neither do its specification or tests. `docs/` names each one
only by its role. The library takes them from its caller as one object (see
`INTEGRATION-SPEC.md`). Tests use obvious dummy values.

The `anycubic_cloud` 3.0 integration supplies them at run time from the
`anycubic-cloud-api` package, which every 2.x install already has. It loads
the named resource files and constants listed in the integration's own spec;
nobody needs to read that package's source for this.

## Log

| Date | Team | Session | Inputs given | Outputs | Did not read the excluded repos |
|---|---|---|---|---|---|
| 2026-09-28 | Specification | Claude (local, Nino's machine) and four local helper sessions | The 2.x integration `hass-anycubic` 2.9.4 and library `anycubic-cloud-api` 0.4.32 source, tests and captured fixtures; field notes | `PROTOCOL.md` (Parts A–D), `INTEGRATION-SPEC.md`, this file, `README.md`, `LICENSE`, CI workflows. Credentials named by role only; scanned against the real values: no match | n/a — specification team |
| 2026-09-28 | Specification | Claude (local) | PR #1 @ `e8f7ad8`, black-box against a real account; the 2.x code for the light-type fact | `ACCEPTANCE.md` (L1, L2); PROTOCOL A §3.7, §4.2 and §4.3 (the rate limit, success code 1), B §2.3.9 (the function names are compatibility data) | n/a — specification team |

## Similarity checks

| Date | Artefact | Compared against | Longest identical run (non-blank, non-comment lines) | Result |
|---|---|---|---|---|
| 2026-09-28 | `src/`, `tests/` @ `e8f7ad8` | 2.x integration, 2.x library, WaresWichall | 25: Agora join-message fields. The same block is in the MIT `homeassistant_petkit` `agora_websocket.py`, which both 3.0 and 2.x adapted with credit. 23: the function-id names, which are compatibility data (BEH §2.14 `supported_functions`) given in PROTOCOL B §2.3.9. 6: SDP field names from the same MIT source. 5: order names from the spec's order table. 4: dataclass field names | Clean |
