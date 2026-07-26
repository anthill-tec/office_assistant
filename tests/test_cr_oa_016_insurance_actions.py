"""CR-OA-016 §S3 — insurance action set gains the regulatory-renewal stages.

The unified skill's Insurance / regulatory-renewal domain teaches the vehicle-RC
re-registration stages (`renew-registration`, `fitness-test`) alongside the policy
actions (PRD §3 "Registration / regulatory" row). `ACTION_SETS` is advisory (unknown
slugs warn but are allowed), but the live `ins_kerala-motor-vehicles-dept_*` row already
carries a `renew-registration` action, so the two slugs belong in the declared vocabulary
to suppress the spurious stderr warning and match what the skill documents.

`renew-registration`/`fitness-test` are not in `ACTION_SETS["insurance"]` yet (CR-OA-016
§S3 is still RED), so this test MUST fail on the missing membership.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")


class InsuranceActionSetTest(unittest.TestCase):
    def _insurance_actions(self):
        sys.path.insert(0, SCRIPTS)
        import store

        return store.ACTION_SETS["insurance"]

    def test_regulatory_renewal_stages_present(self):
        actions = self._insurance_actions()
        self.assertIn("renew-registration", actions)
        self.assertIn("fitness-test", actions)

    def test_existing_policy_actions_preserved(self):
        # negative bound: the additive change must not drop the original policy vocabulary
        actions = self._insurance_actions()
        for slug in ("renew-policy", "pay-premium", "kyc", "claim", "price-compare"):
            self.assertIn(slug, actions)


if __name__ == "__main__":
    unittest.main()
