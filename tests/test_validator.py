from app.config.constants import OpportunityClassification
from app.opportunity.validator import classify


def test_classify_watch():
    assert classify(0.0) == OpportunityClassification.WATCH
    assert classify(0.04) == OpportunityClassification.WATCH


def test_classify_interesting():
    assert classify(0.05) == OpportunityClassification.INTERESTING
    assert classify(0.09) == OpportunityClassification.INTERESTING


def test_classify_good():
    assert classify(0.10) == OpportunityClassification.GOOD


def test_classify_strong():
    assert classify(0.20) == OpportunityClassification.STRONG


def test_classify_exceptional():
    assert classify(0.40) == OpportunityClassification.EXCEPTIONAL
    assert classify(1.5) == OpportunityClassification.EXCEPTIONAL
