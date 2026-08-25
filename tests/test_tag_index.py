import json

import pytest

import spoolman_service as svc


EMPTY = svc.EMPTY_TRAY_UUID


def _legacy(spool_id, value):
  return {"id": spool_id, "extra": {"tag": json.dumps(value)}}


def _native(spool_id, *uids):
  return {"id": spool_id, "tags": [{"uid": uid, "format": "ntag"} for uid in uids]}


@pytest.mark.parametrize("raw,expected", [
  ("04A2B3C4", "04A2B3C4"),
  ("04:a2:b3:c4", "04A2B3C4"),
  ("04-A2-B3-C4", "04A2B3C4"),
  ("04 a2 b3 c4", "04A2B3C4"),
  ("04_a2.b3-c4", "04A2B3C4"),
  ("0x04a2b3c4", "04A2B3C4"),
  ("  04a2b3c4  ", "04A2B3C4"),
  # Everything SpoolMan itself would refuse.
  ("", None),
  (None, None),
  ("::--__", None),
  ("nothex", None),
  ("g4a2b3", None),
  ("A" * 65, None),
])
def test_normalize_uid(raw, expected):
  assert svc.normalize_uid(raw) == expected


def test_a_bambu_tray_uuid_and_an_ntag_uid_are_both_valid():
  assert svc.normalize_uid("CA0B6277B89945279ED757F7A96F2242") is not None
  assert svc.normalize_uid("04:a2:b3:c4:d5:e6:f7") == "04A2B3C4D5E6F7"


def test_a_uuid4_from_write_tag_survives_normalization():
  """/write_tag stores a uuid4, which is hex once the dashes are gone."""
  assert svc.normalize_uid("2f8a1c34-5b6d-4e7f-8a90-b1c2d3e4f506") == \
      "2F8A1C345B6D4E7F8A90B1C2D3E4F506"


def test_legacy_tag_is_found_whatever_the_spelling():
  spool = _legacy(1, "CA0B6277B89945279ED757F7A96F2242")
  index = svc.build_tag_index([spool])

  assert svc.find_spool_by_tag(index, "CA0B6277B89945279ED757F7A96F2242")["id"] == 1
  # Today's code compares the strings exactly, so these two used to miss.
  assert svc.find_spool_by_tag(index, "ca0b6277b89945279ed757f7a96f2242")["id"] == 1
  assert svc.find_spool_by_tag(index, "CA:0B:62:77:B8:99:45:27:9E:D7:57:F7:A9:6F:22:42")["id"] == 1


def test_native_tags_are_found():
  spool = _native(2, "26BE3DEA", "A64119E5")
  index = svc.build_tag_index([spool])

  assert svc.find_spool_by_tag(index, "26BE3DEA")["id"] == 2
  assert svc.find_spool_by_tag(index, "a6:41:19:e5")["id"] == 2


def test_a_native_tag_wins_over_a_legacy_value_on_another_spool():
  """The relation is the source of truth once the server has one."""
  stale = _legacy(1, "26BE3DEA")
  current = _native(2, "26BE3DEA")
  index = svc.build_tag_index([stale, current])

  assert svc.find_spool_by_tag(index, "26BE3DEA")["id"] == 2


def test_the_empty_tray_uuid_never_matches_anything():
  """Every third-party spool and every empty tray reports this."""
  spool = _legacy(1, EMPTY)
  index = svc.build_tag_index([spool, _native(2, EMPTY)])

  assert svc.find_spool_by_tag(index, EMPTY) is None
  assert EMPTY not in index


def test_a_non_hexadecimal_legacy_value_still_matches_exactly():
  """It cannot live in SpoolMan's relation, but it must not stop working."""
  spool = _legacy(1, "shelf-left")
  index = svc.build_tag_index([spool])

  assert svc.find_spool_by_tag(index, "shelf-left")["id"] == 1
  assert svc.find_spool_by_tag(index, "SHELF-LEFT") is None


def test_an_unknown_value_finds_nothing():
  index = svc.build_tag_index([_legacy(1, "AABB"), _native(2, "CCDD")])
  assert svc.find_spool_by_tag(index, "EEFF") is None
  assert svc.find_spool_by_tag(index, "") is None
  assert svc.find_spool_by_tag(index, None) is None


def test_spool_tag_values_lists_native_first_then_legacy():
  spool = _native(1, "26BE3DEA", "A64119E5")
  spool["extra"] = {"tag": json.dumps("6AB8A4FE")}

  assert svc.spool_tag_values(spool) == ["26BE3DEA", "A64119E5", "6AB8A4FE"]
  assert svc.spool_has_tag(spool)


def test_a_cleared_tag_counts_as_untagged():
  """extra.tag is stored as an encoded empty string when it is cleared."""
  spool = _legacy(1, "")

  assert svc.spool_tag_values(spool) == []
  assert not svc.spool_has_tag(spool)


def test_a_spool_with_neither_is_untagged():
  assert not svc.spool_has_tag({"id": 1})
  assert not svc.spool_has_tag({"id": 1, "extra": {}, "tags": []})
