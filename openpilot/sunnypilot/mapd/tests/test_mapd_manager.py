#!/usr/bin/env python3
"""Country/state filtering for the OSM download request.

The "all" marker is a user-facing convenience and arrives in any case; the filter
must normalise it without mutating the caller's lists and without raising.
"""

from openpilot.sunnypilot.mapd.mapd_manager import filter_nations_and_states


def test_lowercase_all_is_dropped_case_insensitively():
  assert filter_nations_and_states(["US"], ["all"]) == (["US"], [])
  assert filter_nations_and_states(["US"], ["All"]) == (["US"], [])
  assert filter_nations_and_states(["CA"], ["all"]) == (["CA"], [])


def test_a_specific_us_state_drops_the_country_entry():
  assert filter_nations_and_states(["US"], ["CA"]) == ([], ["CA"])


def test_inputs_are_not_mutated():
  nations = ["US"]
  states = ["all"]
  filter_nations_and_states(nations, states)
  assert nations == ["US"]
  assert states == ["all"]
