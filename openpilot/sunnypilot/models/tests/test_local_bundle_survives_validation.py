
from types import SimpleNamespace

from openpilot.sunnypilot.models import helpers


def _bundle(ref, internal="amd_stock.pkl"):
  return SimpleNamespace(ref=ref, internalName=internal, minimumSelectorVersion=19,
                         runner=0, models=[])


class TestLocalBundlesSurviveCatalogueValidation:
  def test_local_ref_is_never_reset(self):
    assert helpers._bundle_needs_reset(_bundle("local:igpu-small:amd_stock.pkl"), []) is False

  def test_local_ref_survives_a_non_empty_catalogue(self):
    catalogue = [SimpleNamespace(ref="0394d728", internalName="TSFDOM")]
    assert helpers._bundle_needs_reset(_bundle("local:igpu-big"), catalogue) is False

  def test_catalogue_ref_missing_from_the_list_still_resets(self):
    catalogue = [SimpleNamespace(ref="other-ref", internalName="other")]
    assert helpers._bundle_needs_reset(_bundle("0394d728", internal="TSFDOM"), catalogue) is True

  def test_catalogue_ref_present_does_not_reset(self):
    b = _bundle("0394d728", internal="TSFDOM")
    catalogue = [SimpleNamespace(ref="0394d728", internalName="TSFDOM",
                                 minimumSelectorVersion=19, runner=0, models=[])]
    assert helpers._bundle_needs_reset(b, catalogue) is False
