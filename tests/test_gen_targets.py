"""Regression guards for tools/gen_targets.py leg selection.

The bug these exist to prevent: gen_targets used to pick
min(tx_freq, rx_freq) on the theory that "the lower leg is the repeater
output".  That is offset-sign roulette -- correct on 70cm, where the
input sits +5 MHz above the output, and wrong on 2m and 220, where the
input sits below it.  Every 2m and 220 repeater in a generated target
list was therefore pointed at its INPUT, where an observer hears only
nearby users' uplinks and never the machine, so those targets read dead
no matter how busy the repeater was.

ssrf-lite is radio-centric (see its commits "docs: clarify tx/rx
perspective is radio-centric" and "fix(cfmc): tx/rx were station-centric,
reversed on all 7 chains"): rx is what YOUR RADIO receives, i.e. the
repeater output.  So the correct rule is simply "always take rx".

Ground truth below is WA9ORC's own published data:
    2m   146.760 output, -600 kHz input (146.160), PL 107.2
    70cm 443.750 output, +5 MHz input (448.750), PL 114.8
"""
import importlib.util, os, sys, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOL = os.path.join(_HERE, "..", "tools", "gen_targets.py")
_spec = importlib.util.spec_from_file_location("gen_targets", _TOOL)
gen_targets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_targets)


def _doc(chains, assignments, stations=None, orgs=None):
    return {
        "organizations": orgs or [{"id": "org_cfmc", "name": "Chicago FM Club"}],
        "stations": stations or [{"id": "stn_a", "organization_id": "org_cfmc"}],
        "rf_chains": chains,
        "assignments": assignments,
    }


def _chain(cid, tx, rx, station="stn_a", mode=None):
    # mode is required: gen_targets maps mode.type -> decoder and skips
    # any assignment it cannot map.
    return {"id": cid, "station_id": station, "tx": tx, "rx": rx,
            "mode": mode or {"type": "FM"}}


def _asgn(aid, cid, name, usage="repeater"):
    return {"id": aid, "rf_chain_id": cid, "channel_name": name,
            "usage": usage}


class TestLegSelection(unittest.TestCase):

    def _freqs(self, doc, **kw):
        t, _ = gen_targets.targets_from_doc(doc, 2, ("repeater", "simplex"),
                                            **kw)
        return {x["name"]: x["freq_mhz"] for x in t}

    def test_2m_takes_output_not_input(self):
        # Input BELOW output: the case min() got wrong.
        doc = _doc([_chain("c", {"freq_mhz": 146.16}, {"freq_mhz": 146.76})],
                   [_asgn("a", "c", "WA9ORC 2m")])
        self.assertEqual(self._freqs(doc)["WA9ORC 2m"], 146.76)

    def test_70cm_takes_output(self):
        # Input ABOVE output: the case min() got right by luck.
        doc = _doc([_chain("c", {"freq_mhz": 448.75}, {"freq_mhz": 443.75})],
                   [_asgn("a", "c", "WA9ORC 70cm")])
        self.assertEqual(self._freqs(doc)["WA9ORC 70cm"], 443.75)

    def test_220_takes_output_not_input(self):
        doc = _doc([_chain("c", {"freq_mhz": 222.50}, {"freq_mhz": 224.10})],
                   [_asgn("a", "c", "WA9ORC 220")])
        self.assertEqual(self._freqs(doc)["WA9ORC 220"], 224.10)

    def test_simplex_with_freqless_tx_block_is_kept(self):
        # Conventional simplex states the frequency once, on rx, and
        # carries a tx block holding only emission.  The old
        # "tx_leg or rx_leg" fallback tested dict truthiness, selected
        # that frequency-less tx block and DROPPED the record -- losing
        # every simplex channel in the catalog silently.
        doc = _doc([_chain("c", {"emission": "11K0F3E"},
                           {"freq_mhz": 154.16})],
                   [_asgn("a", "c", "Evanston Fire Old", usage="simplex")])
        self.assertEqual(self._freqs(doc)["Evanston Fire Old"], 154.16)

    def test_tx_only_record_falls_back_to_tx(self):
        doc = _doc([_chain("c", {"freq_mhz": 151.0}, {})],
                   [_asgn("a", "c", "TX only", usage="simplex")])
        self.assertEqual(self._freqs(doc)["TX only"], 151.0)

    def test_record_with_no_frequency_is_skipped(self):
        doc = _doc([_chain("c", {"emission": "11K0F3E"}, {})],
                   [_asgn("a", "c", "Nothing", usage="simplex")])
        t, skipped = gen_targets.targets_from_doc(doc, 2, ("simplex",))
        self.assertEqual(t, [])
        self.assertEqual(skipped, [("a", "no freq_mhz")])


class TestOrgFilter(unittest.TestCase):
    """One ssrf-lite file often holds every agency in a town."""

    DOC = _doc(
        [_chain("c_fd", {"freq_mhz": 159.4275}, {"freq_mhz": 155.6925},
                station="stn_fd"),
         _chain("c_pd", {"freq_mhz": 477.4875}, {"freq_mhz": 472.4875},
                station="stn_pd")],
        [_asgn("a_fd", "c_fd", "Evanston Fire New"),
         _asgn("a_pd", "c_pd", "Evanston Police Alt")],
        stations=[{"id": "stn_fd", "organization_id": "org_efd"},
                  {"id": "stn_pd", "organization_id": "org_epd"}],
        orgs=[{"id": "org_efd", "name": "Evanston Fire Department"},
              {"id": "org_epd", "name": "Evanston Police Department"}])

    def _names(self, org):
        t, _ = gen_targets.targets_from_doc(self.DOC, 1, ("repeater",),
                                            org=org)
        return [x["name"] for x in t]

    def test_filters_to_one_agency_by_org_id(self):
        self.assertEqual(self._names("efd"), ["Evanston Fire New"])

    def test_filters_by_org_name_substring(self):
        self.assertEqual(self._names("police"), ["Evanston Police Alt"])

    def test_no_org_returns_everything(self):
        self.assertEqual(len(self._names(None)), 2)

    def test_unmatched_org_returns_nothing(self):
        self.assertEqual(self._names("nosuchagency"), [])


if __name__ == "__main__":
    unittest.main()
