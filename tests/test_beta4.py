"""Beta 4 field reports (hass-anycubic-next ACCEPTANCE D21-D22)."""

from __future__ import annotations

import pytest

from anycubic_cloud_client.models import PrinterDetail, parse_ace_units

from .payloads import detail

# -- D21: chamber temperature in the printer record -----------------------------


@pytest.mark.parametrize(
    ("parameter", "chamber"),
    [
        ({"curr_hotbed_temp": 28, "curr_nozzle_temp": 31, "curr_chamber_temp": 32}, 32),
        ({"curr_hotbed_temp": 28, "curr_nozzle_temp": 31, "curr_chamber_temp": 0}, 0),
        ({"curr_hotbed_temp": 28, "curr_nozzle_temp": 31}, None),
        ({"curr_chamber_temp": None}, None),
        (None, None),
    ],
)
def test_chamber_temperature_from_the_record(
    parameter: object, chamber: float | None
) -> None:
    printer = PrinterDetail.from_data(detail(parameter=parameter))
    assert printer.chamber_temp == chamber


# -- D22: an ACE's own humidity (read since 0.1.0) --------------------------------


def test_ace_humidity_from_the_record() -> None:
    units = parse_ace_units([{"id": 0, "humidity": 58}, {"id": 1, "humidity": 0}])
    assert [u.humidity for u in units] == [58, 0]
    assert parse_ace_units([{"id": 0}])[0].humidity is None
