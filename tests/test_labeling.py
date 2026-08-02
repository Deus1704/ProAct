"""Tests for the labelling pipeline.

These deliberately test the *statistical* properties, not just that the code
runs. A labelling pipeline that executes without error but produces a biased
estimate is worse than one that crashes, because you will believe it.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PROACT_LABEL_SECRET", "test-secret")

from labeling import forms, ingest, metrics  # noqa: E402
from labeling.capture import NEARMISS_MARGIN, classify, shadow_policy  # noqa: E402
from labeling.sample import materialise, plan_batch  # noqa: E402
from labeling.schema import (  # noqa: E402
    Batch,
    Candidate,
    Participant,
    init_db,
    insert_batch,
    insert_candidate,
    labelled_rows,
    upsert_participant,
    utc_now_iso,
)

from proactive_assistant_app.agents import TriggerPolicy  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "labels.db")
    init_db(path)
    upsert_participant(Participant("p1", utc_now_iso()), path)
    return path


def _cand(db, stratum, n=1, fired=False, conf=0.7, start=0):
    made = []
    for i in range(n):
        cid = insert_candidate(
            Candidate(
                participant_id="p1",
                domain="food",
                decided_at=f"2026-03-0{1 + (start + i) % 9}T1{(start + i) % 10}:00:00+00:00",
                stratum=stratum,
                fired_prod=fired,
                reason="fire" if fired else stratum.lower(),
                confidence=conf,
                threshold=0.55,
                pattern_ref=f"order_patterns:{start + i}",
            ),
            db,
        )
        made.append(cid)
    return made


# ---------------------------------------------------------------------------
class TestTokens:
    def test_roundtrip(self):
        tok, _ = forms.mint_token("p1", "daily")
        payload = forms.verify_token(tok)
        assert payload and payload["p"] == "p1" and payload["k"] == "daily"

    def test_tampering_is_detected(self):
        """The participant CAN edit the prefilled field. The guarantee is that
        editing it is detectable, not that it is impossible."""
        tok, _ = forms.mint_token("p1", "daily")
        body, sig = tok.split(".")
        forged = forms._b64(b'{"e":9999999999,"k":"daily","n":"x","p":"someone_else"}')
        assert forms.verify_token(f"{forged}.{sig}") is None

    def test_expired_token_rejected(self):
        tok, _ = forms.mint_token("p1", "daily", ttl_hours=-1)
        assert forms.verify_token(tok) is None

    def test_refuses_to_mint_without_a_secret(self, monkeypatch):
        monkeypatch.delenv(forms.TOKEN_SECRET_ENV, raising=False)
        with pytest.raises(RuntimeError):
            forms.mint_token("p1", "daily")


# ---------------------------------------------------------------------------
class TestStratification:
    def test_fired_maps_to_fired_strata(self):
        assert classify(True, "fire", 0.9, 0.6, soft=False) == "S1_FIRED_STRONG"
        assert classify(True, "fire", 0.62, 0.6, soft=False) == "S2_FIRED_SOFT"
        assert classify(True, "fire", 0.9, 0.6, soft=True) == "S2_FIRED_SOFT"

    def test_reason_drives_the_stratum(self):
        assert classify(False, "cooldown:ride:45m", 0.9, 0.6, False) == "S4_COOLDOWN"
        assert classify(False, "suppressed:dismissals=4", 0.9, 0.6, False) == "S5_SUPPRESSED"
        assert classify(False, "already_booked_today", 0.9, 0.6, False) == "S5_SUPPRESSED"

    def test_nearmiss_only_within_the_margin(self):
        just_under = 0.6 - NEARMISS_MARGIN / 2
        far_under = 0.6 - NEARMISS_MARGIN * 2
        assert classify(False, "below_threshold:x", just_under, 0.6, False) == "S3_NEARMISS"
        assert classify(False, "below_threshold:x", far_under, 0.6, False) == "S6_CONTROL"

    def test_shadow_policy_is_strictly_more_permissive(self):
        prod = TriggerPolicy()
        shadow = shadow_policy(prod)
        assert shadow.ride_cooldown_minutes == 0
        assert shadow.food_cooldown_minutes == 0
        assert shadow.ride_confidence_threshold < prod.ride_confidence_threshold
        assert shadow.food_confidence_threshold < prod.food_confidence_threshold


# ---------------------------------------------------------------------------
class TestSampling:
    def test_inclusion_probability_matches_what_was_drawn(self, db):
        _cand(db, "S6_CONTROL", n=100)
        plan = plan_batch("p1", allocation={"S6_CONTROL": 5}, seed=1, labels_db=db)
        assert len(plan.drawn["S6_CONTROL"]) == 5
        assert plan.inclusion_prob["S6_CONTROL"] == pytest.approx(5 / 100)

    def test_weights_are_the_inverse_of_pi(self, db):
        _cand(db, "S6_CONTROL", n=50)
        plan = plan_batch("p1", allocation={"S6_CONTROL": 5}, seed=1, labels_db=db)
        bid = insert_batch(Batch("p1", "daily", utc_now_iso(), "tok-w", "2099-01-01"), db)
        items = materialise(plan, bid, seed=1, labels_db=db)
        assert all(i.inclusion_prob == pytest.approx(0.1) for i in items)
        assert all(1 / i.inclusion_prob == pytest.approx(10.0) for i in items)

    def test_excluded_candidates_are_never_redrawn(self, db):
        ids = _cand(db, "S3_NEARMISS", n=10)
        first = plan_batch("p1", allocation={"S3_NEARMISS": 4}, seed=1, labels_db=db)
        drawn = set(first.drawn["S3_NEARMISS"])
        second = plan_batch(
            "p1", allocation={"S3_NEARMISS": 4}, seed=2,
            exclude_candidate_ids=drawn, labels_db=db,
        )
        assert not (drawn & set(second.drawn["S3_NEARMISS"]))

    def test_pi_accounts_for_exclusions(self, db):
        """pi must be computed over the ELIGIBLE pool. Using the raw stratum size
        would understate pi and inflate every weight in later batches."""
        _cand(db, "S3_NEARMISS", n=10)
        first = plan_batch("p1", allocation={"S3_NEARMISS": 5}, seed=1, labels_db=db)
        second = plan_batch(
            "p1", allocation={"S3_NEARMISS": 5}, seed=2,
            exclude_candidate_ids=set(first.drawn["S3_NEARMISS"]), labels_db=db,
        )
        assert second.inclusion_prob["S3_NEARMISS"] == pytest.approx(1.0)

    def test_items_are_persisted_with_ids(self, db):
        """Regression: executemany cannot return per-row ids, which left every
        item_id None and made responses unattributable."""
        _cand(db, "S1_FIRED_STRONG", n=4, fired=True)
        plan = plan_batch("p1", allocation={"S1_FIRED_STRONG": 3}, seed=1, labels_db=db)
        bid = insert_batch(Batch("p1", "daily", utc_now_iso(), "tok-i", "2099-01-01"), db)
        items = materialise(plan, bid, seed=1, labels_db=db)
        assert items and all(i.item_id is not None for i in items)


# ---------------------------------------------------------------------------
class TestIngest:
    def _batch_with_items(self, db, stratum="S1_FIRED_STRONG", n=3, fired=True):
        _cand(db, stratum, n=n, fired=fired)
        plan = plan_batch("p1", allocation={stratum: n}, seed=1, labels_db=db)
        tok, exp = forms.mint_token("p1", "daily")
        bid = insert_batch(Batch("p1", "daily", utc_now_iso(), tok, exp), db)
        return tok, materialise(plan, bid, seed=1, labels_db=db)

    def test_records_answers(self, db):
        tok, items = self._batch_with_items(db)
        n = ingest.record_response(
            tok, {i.item_id: {"welcome": "Useful", "wanted": "Yes"} for i in items},
            labels_db=db,
        )
        assert n == len(items)

    def test_bad_token_rejected(self, db):
        _tok, items = self._batch_with_items(db)
        with pytest.raises(ingest.IngestError):
            ingest.record_response("garbage.sig", {items[0].item_id: {"welcome": "Useful"}}, labels_db=db)

    def test_item_from_another_batch_rejected(self, db):
        tok_a, items_a = self._batch_with_items(db, n=2)
        _tok_b, items_b = self._batch_with_items(db, stratum="S3_NEARMISS", n=2, fired=False)
        with pytest.raises(ingest.IngestError):
            ingest.record_response(tok_a, {items_b[0].item_id: {"welcome": "Useful"}}, labels_db=db)

    def test_resubmission_overwrites_rather_than_appends(self, db):
        """A double-counted label in a high-weight stratum can move the headline
        estimate by several points, so replay must be idempotent."""
        tok, items = self._batch_with_items(db)
        target = items[0].item_id
        ingest.record_response(tok, {target: {"welcome": "Useful"}}, labels_db=db)
        ingest.record_response(tok, {target: {"welcome": "Annoying"}}, labels_db=db)
        rows = [r for r in labelled_rows(db) if r["item_id"] == target]
        assert len(rows) == 1 and rows[0]["welcome"] == "annoying"

    def test_answers_are_normalised(self, db):
        tok, items = self._batch_with_items(db)
        ingest.record_response(
            tok, {items[0].item_id: {"welcome": "  USEFUL ", "wanted": "Don't remember"}},
            labels_db=db,
        )
        row = [r for r in labelled_rows(db) if r["item_id"] == items[0].item_id][0]
        assert row["welcome"] == "useful" and row["wanted"] == "dont_remember"


# ---------------------------------------------------------------------------
class TestEstimation:
    def test_weighting_changes_the_answer(self):
        """The point of the whole design. Two strata with very different weights:
        counting rows and weighting rows must disagree, or the weights are not
        doing anything."""
        grouped = {
            # 10 fired items, weight 1, all useful
            "S1_FIRED_STRONG": [(True, True, 1.0)] * 10,
            # 10 sampled from a huge stratum, weight 50, none useful
            "S6_CONTROL": [(False, True, 50.0)] * 10,
        }
        point, lo, hi, n = metrics.stratified_bootstrap_ratio(grouped, n_boot=200)
        unweighted = 10 / 20
        assert n == 20
        assert point < unweighted / 2, "weighting must pull the estimate toward the big stratum"

    def test_point_estimate_lies_inside_its_interval(self):
        """Regression: a weighted point with an unweighted Wilson interval could
        place the estimate outside its own CI."""
        grouped = {
            "S1_FIRED_STRONG": [(True, True, 8.0)] * 12 + [(False, True, 8.0)] * 4,
            "S2_FIRED_SOFT": [(True, True, 4.5)] * 2 + [(False, True, 4.5)] * 4,
        }
        point, lo, hi, _ = metrics.stratified_bootstrap_ratio(grouped, n_boot=500)
        assert lo <= point <= hi

    def test_recall_interval_is_not_degenerate(self):
        """Whether a moment fired is DETERMINED by its stratum, so within-stratum
        variance is zero. A stratified-binomial formula returns a zero-width
        interval here; the bootstrap must not."""
        # The FULL answered sample per stratum, as (in_numerator, in_denominator,
        # weight). Only some rows in each stratum are "wanted", and that
        # variation is the entire source of the uncertainty. Pre-filtering to the
        # wanted subset is what collapsed the interval to a point.
        grouped = {
            "S1_FIRED_STRONG": [(True, True, 8.0)] * 11 + [(False, False, 8.0)] * 5,
            "S3_NEARMISS": [(False, True, 8.67)] * 5 + [(False, False, 8.67)] * 16,
            "S6_CONTROL": [(False, True, 57.0)] * 2 + [(False, False, 57.0)] * 97,
        }
        point, lo, hi, _ = metrics.stratified_bootstrap_ratio(grouped, n_boot=800)
        assert hi > lo, "recall interval collapsed to a point"
        assert 0.0 < point < 1.0

    def test_zero_events_uses_rule_of_three(self):
        """0 observed events is not certainty of zero. Upper bound ~3/n."""
        grouped = {"S1_FIRED_STRONG": [(False, True, 1.0)] * 22}
        point, lo, hi, n = metrics.stratified_bootstrap_ratio(grouped, n_boot=200)
        assert point == 0.0 and lo == 0.0
        assert hi == pytest.approx(3 / 22, rel=1e-6)

    def test_effective_n_not_estimable_for_one_cluster(self):
        assert metrics.effective_n(200, [200]) != metrics.effective_n(200, [200])  # NaN

    def test_effective_n_shrinks_with_clustering(self):
        n = metrics.effective_n(200, [100, 100], icc=0.15)
        assert n < 200 and n > 0

    def test_kappa_detects_agreement_and_chance(self):
        perfect = [("useful", "useful")] * 10 + [("neutral", "neutral")] * 10
        assert metrics.cohens_kappa(perfect) == pytest.approx(1.0)
        # One category dominating: raw agreement is high, kappa should not be
        alternating = [("neutral", "neutral")] * 18 + [("useful", "annoying")] * 2
        assert metrics.cohens_kappa(alternating) < 0.5

    def test_wilson_stays_in_bounds_at_the_extreme(self):
        lo, hi = metrics.wilson_interval(200, 200)
        assert 0.0 <= lo <= 1.0 and hi <= 1.0


# ---------------------------------------------------------------------------
class TestExport:
    def test_export_matches_eval_schema_and_carries_weights(self, db):
        _cand(db, "S1_FIRED_STRONG", n=2, fired=True)
        plan = plan_batch("p1", allocation={"S1_FIRED_STRONG": 2}, seed=1, labels_db=db)
        tok, exp = forms.mint_token("p1", "daily")
        bid = insert_batch(Batch("p1", "daily", utc_now_iso(), tok, exp), db)
        items = materialise(plan, bid, seed=1, labels_db=db)
        ingest.record_response(
            tok,
            {
                items[0].item_id: {"welcome": "Useful", "wanted": "Yes"},
                items[1].item_id: {"welcome": "Annoying", "wanted": "No"},
            },
            labels_db=db,
        )
        payload = ingest.export_sessions(labelled_rows(db))
        assert payload["meta"]["count"] == 2
        assert payload["meta"]["weighted"] is True
        labels = sorted(s["label"] for s in payload["sessions"])
        assert labels == [0, 1]
        for s in payload["sessions"]:
            # The keys eval/run_eval.py requires
            for key in ("session_id", "domain", "label", "evaluate_at", "rides", "orders"):
                assert key in s
            # ...plus the weight, whose absence silently biases every estimate
            assert s["inclusion_prob"] > 0

    def test_unanswered_items_are_excluded(self, db):
        _cand(db, "S1_FIRED_STRONG", n=3, fired=True)
        plan = plan_batch("p1", allocation={"S1_FIRED_STRONG": 3}, seed=1, labels_db=db)
        tok, exp = forms.mint_token("p1", "daily")
        bid = insert_batch(Batch("p1", "daily", utc_now_iso(), tok, exp), db)
        items = materialise(plan, bid, seed=1, labels_db=db)
        ingest.record_response(tok, {items[0].item_id: {"welcome": "Useful"}}, labels_db=db)
        payload = ingest.export_sessions(labelled_rows(db))
        assert payload["meta"]["count"] == 1


class TestPrivacy:
    def test_item_cards_carry_no_coordinates_or_addresses(self):
        """Card text reaches Google. It must contain nothing that could identify
        a location beyond a coarse nickname."""
        ctx = json.dumps(
            {"domain": "food", "weekday": "Tuesday", "time": "20:15",
             "bin": 81, "place_label": "Office", "restaurant_label": "the usual place",
             "confidence": 0.81}
        )
        card = forms.ItemCard.from_context(1, ctx)
        blob = (card.title + card.detail).lower()
        for leak in ("lat", "lng", "12.9", "77.5", "street", "road", "http"):
            assert leak not in blob
