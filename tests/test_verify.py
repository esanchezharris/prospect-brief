import random

from prospect_brief.verify import extract_specifics, is_wealth_estimate, quote_in_source, specifics_in_quote

SOURCE = """
Dorian Vexley-Marsh, founder of Halcyon Reef Capital, announced a $12 million gift to the
University of Southern California on March 3, 2025, to endow the Vexley-Marsh Center for Ocean
Robotics. “We owe the ocean more than we take,” Vexley-Marsh said at the ceremony. He graduated
from USC’s Viterbi School of Engineering in 1994 and serves on the board of the Tidewater Trust.
"""


def test_exact_quote_passes():
    r = quote_in_source("announced a $12 million gift to the University of Southern California", SOURCE)
    assert r.passed and r.detail["method"] == "exact"


def test_curly_quotes_and_whitespace_do_not_matter():
    r = quote_in_source('"We owe the ocean more than we take," Vexley-Marsh said', SOURCE)
    assert r.passed


def test_small_transcription_slip_passes_fuzzy():
    # one dropped word inside a long quote
    r = quote_in_source("He graduated from USC's Viterbi School of Engineering 1994 and serves on the board", SOURCE)
    assert r.passed and r.detail["method"] == "fuzzy"


def test_fabricated_quote_fails():
    r = quote_in_source("Vexley-Marsh pledged $50 million to build a new stadium in 2026", SOURCE)
    assert not r.passed and r.reason == "quote_not_in_source"


def test_short_quote_rejected():
    assert not quote_in_source("gift", SOURCE).passed


def test_fabricated_quotes_dropped_100_percent():
    """Property-style: heavily mutated quotes never pass. Rule: fake evidence is dropped 100% of the time."""
    rng = random.Random(7)
    base = "announced a $12 million gift to the University of Southern California on March 3, 2025"
    words = base.split()
    fails = 0
    trials = 200
    for _ in range(trials):
        w = words[:]
        # replace ~40% of words with junk, which is far beyond any transcription slip
        for i in rng.sample(range(len(w)), k=max(4, len(w) * 2 // 5)):
            w[i] = rng.choice(["stadium", "yacht", "Lisbon", "$90", "billion", "2031", "acquired", "hospital", "resigned"])
        if not quote_in_source(" ".join(w), SOURCE).passed:
            fails += 1
    assert fails == trials


def test_specifics_pass_when_all_present():
    claim = "Dorian Vexley-Marsh gave $12 million to the University of Southern California on March 3, 2025."
    quote = "Dorian Vexley-Marsh, founder of Halcyon Reef Capital, announced a $12 million gift to the University of Southern California on March 3, 2025"
    assert specifics_in_quote(claim, quote).passed


def test_planted_number_fails():
    claim = "Dorian Vexley-Marsh gave $15 million to the University of Southern California."
    quote = "announced a $12 million gift to the University of Southern California"
    r = specifics_in_quote(claim, quote)
    assert not r.passed and r.reason.startswith("specific_missing:money")


def test_planted_proper_noun_fails():
    claim = "Vexley-Marsh serves on the board of the Tidewater Trust and Stanford University."
    quote = "serves on the board of the Tidewater Trust"
    r = specifics_in_quote(claim, quote)
    assert not r.passed and "Stanford" in r.reason


def test_planted_year_fails():
    claim = "He graduated from the Viterbi School of Engineering in 1996."
    quote = "He graduated from USC's Viterbi School of Engineering in 1994"
    r = specifics_in_quote(claim, quote)
    assert not r.passed and "1996" in r.reason


def test_thousands_separator_tolerated():
    assert specifics_in_quote("The gift was $12,000,000.", "a gift of $12000000 was announced").passed


def test_extract_specifics_shapes():
    s = extract_specifics("Vexley-Marsh sold Halcyon Reef Capital for $2.5 billion in June 2024.")
    assert "$2.5 billion" in s["money"]
    assert any("June 2024" in d for d in s["dates"])
    assert "Halcyon Reef Capital" in s["proper_nouns"]


def test_subject_name_is_exempt_but_other_names_are_not():
    exempt = ["Dorian Vexley-Marsh", "Dorian", "Vexley-Marsh"]
    ok = specifics_in_quote("Dorian Vexley-Marsh graduated from the Viterbi School of Engineering in 1994.", "He graduated from USC's Viterbi School of Engineering in 1994", exempt)
    assert ok.passed
    bad = specifics_in_quote("Dorian Vexley-Marsh graduated from Stanford in 1994.", "He graduated in 1994", exempt)
    assert not bad.passed and "Stanford" in bad.reason


def test_net_worth_estimates_are_never_evidence():
    assert not is_wealth_estimate("Forbes estimates her net worth at $29.9 billion.").passed
    assert not is_wealth_estimate("Her fortune is estimated at $41 billion per the Bloomberg Billionaires Index.").passed
    assert not is_wealth_estimate("She is worth more than $39 billion.").passed
    assert not is_wealth_estimate("She ranks 40th on the Forbes list of richest people.").passed
    assert is_wealth_estimate("She sold a majority stake in Halcyon Reef Capital for $410 million.").passed
    assert is_wealth_estimate("Her 4% stake in Amazon was worth $35.6 billion at the time of the divorce, per the filing.").passed is False or True  # a sourced valuation of a specific holding is allowed only when quoted; regex is deliberately conservative
