"""Model Status row: which bundle slot the row reports as driving.

The row names one bundle per slot, and green marks the slot modeld actually
loads. A locally compiled iGPU build bypasses both slots but is what modeld
reads, so the big slot has to report it; naming the chestnut bundle there made
the row say "big: BMRLNAP Model v4 (Default)" directly under a Big Model row
that showed the local artifact, on a device where no bundle is downloaded.
"""

from types import SimpleNamespace
from unittest.mock import patch

from openpilot.selfdrive.ui.sunnypilot import model_info


LOCAL_CARRY = ("igpu", "amd_driving_big_tinygrad.pkl", "big_driving_supercombo @f22b8b34 h16384")
BUNDLES = {
  "qcom": SimpleNamespace(internalName="CD210"),
  "chestnut": SimpleNamespace(internalName="BMRLNAP_Model_v4"),
}


def slot(source, carry, bundles=None):
  bundles = BUNDLES if bundles is None else bundles
  with patch.object(model_info, "carrying_model", return_value=carry), \
       patch.object(model_info, "get_selected_bundle", side_effect=lambda _params, src: bundles.get(src)), \
       patch.object(model_info, "default_model_name", side_effect=lambda src: f"default-{src} (Default)"):
    return model_info.slot_entry(source)


class TestLocalBuild:
  def test_big_slot_reports_the_local_artifact(self):
    name, driving = slot("chestnut", LOCAL_CARRY)
    assert name == "big_driving_supercombo @f22b8b34 h16384"
    assert driving is True

  def test_big_slot_never_names_a_bundle_that_is_not_loaded(self):
    """The chestnut bundle is still selected in params; it must not show up."""
    name, _ = slot("chestnut", LOCAL_CARRY)
    assert name != BUNDLES["chestnut"].internalName

  def test_small_slot_stays_idle(self):
    name, driving = slot("qcom", LOCAL_CARRY)
    assert driving is False
    assert name == "CD210"

  def test_local_artifact_without_a_pretty_name_falls_back_to_the_file(self):
    name, driving = slot("chestnut", ("igpu", "amd_driving_big_tinygrad.pkl", None))
    assert name == "amd_driving_big_tinygrad.pkl"
    assert driving is True


class TestBundleSlots:
  def test_small_slot_driving_when_the_small_bundle_carries(self):
    name, driving = slot("qcom", ("qcom", "CD210", "CD210 (Default)"))
    assert (name, driving) == ("CD210", True)

  def test_big_slot_idle_while_the_small_one_drives(self):
    name, driving = slot("chestnut", ("qcom", "CD210", "CD210 (Default)"))
    assert (name, driving) == ("BMRLNAP_Model_v4", False)

  def test_big_slot_driving_when_its_own_pick_carries(self):
    name, driving = slot("chestnut", ("chestnut", "BMRLNAP_Model_v4", "BMRLNAP Model v4"))
    assert (name, driving) == ("BMRLNAP_Model_v4", True)

  def test_defaults_are_named_when_a_slot_has_no_bundle(self):
    name, driving = slot("chestnut", ("qcom", "CD210", "CD210 (Default)"), bundles={"qcom": BUNDLES["qcom"]})
    assert name == "default-chestnut (Default)"
    assert driving is False

  def test_nothing_driving_leaves_both_slots_idle(self):
    for source in ("qcom", "chestnut"):
      assert slot(source, (None, None, None))[1] is False
