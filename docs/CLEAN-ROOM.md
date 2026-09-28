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
| 2026-09-28 | Implementation | Claude cloud session (routine, v0.1.0) | This repository's `docs/` (`PROTOCOL.md` Parts A–D, `INTEGRATION-SPEC.md`, `CLEAN-ROOM.md`, `QUESTIONS.md`), `README.md` and CI workflows; `Nino6689/anycubic-lan` (`src/anycubic_lan/` `__init__.py`, `reports.py`, `models.py`, `exceptions.py`, `client.py`; `pyproject.toml`; `README.md`) and its PyPI wheel 0.1.0 (compared: identical to that source); `Nino6689/hass-anycubic-next` `docs/BEHAVIOUR.md` (§2.17, §2.18, §5, §6, §7, §9), `docs/COMPAT.md` (§6), `docs/CLOUD.md`, `docs/DECISIONS.md`; `Jezza34000/homeassistant_petkit` at commit `a749d54` (`LICENSE`, `custom_components/petkit/agora_api.py`, `agora_websocket.py`, `agora_sdp.py`, `webrtc_common.py`; `camera.py` searched for its Agora calls only) | `src/anycubic_cloud_client/` (all modules; `agora.py` and `agora_sdp.py` adapted from petkit with its MIT notice), `tests/`, `pyproject.toml`, `README.md`, `docs/QUESTIONS.md` Q1–Q15, this row. The Agora SDK's npm package was not fetched (Q6) | Yes |

## Similarity checks

| Date | Artefact | Compared against | Longest identical run (non-blank, non-comment lines) | Result |
|---|---|---|---|---|
