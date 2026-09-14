"""
Tests for ProtocolParser.parse_where_reply — the WHERE# command reply
format confirmed live against a real G900LS J16-4G (see the method's
doc comment in app/protocol_parser.py for the captured sample).
"""

from datetime import datetime

from app.protocol_parser import ProtocolParser


def test_parses_confirmed_live_sample():
    parser = ProtocolParser()
    content = "LastPosition! Lati:S1.943835,E30.094658,Course:11,Speed:0.00,DateTime:2026-09-14 14:13:27"

    result = parser.parse_where_reply(content)

    assert result == {
        'latitude': -1.943835,
        'longitude': 30.094658,
        'course': 11,
        'speed': 0.0,
        'timestamp': datetime(2026, 9, 14, 14, 13, 27),
    }


def test_north_east_hemisphere_not_negated():
    parser = ProtocolParser()
    content = "LastPosition! Lati:N1.5,E30.1,Course:90,Speed:12.50,DateTime:2026-01-01 00:00:00"

    result = parser.parse_where_reply(content)

    assert result['latitude'] == 1.5
    assert result['longitude'] == 30.1


def test_north_west_hemisphere_negates_longitude_only():
    parser = ProtocolParser()
    content = "LastPosition! Lati:N1.5,W30.1,Course:0,Speed:0,DateTime:2026-01-01 00:00:00"

    result = parser.parse_where_reply(content)

    assert result['latitude'] == 1.5
    assert result['longitude'] == -30.1


def test_returns_none_for_unrelated_content():
    parser = ProtocolParser()
    # A STATUS# reply, or any non-location command response, must never be
    # mistaken for a location fix.
    assert parser.parse_where_reply("Battery:100%,GSM:4,GPS:1,ACC:ON") is None


def test_returns_none_for_empty_or_garbage_content():
    parser = ProtocolParser()
    assert parser.parse_where_reply("") is None
    assert parser.parse_where_reply(None) is None
    assert parser.parse_where_reply("No Fix") is None
