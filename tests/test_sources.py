from prospect_brief.sources.edgar import name_matches_entity, parse_form4, render_form4
from prospect_brief.sources.propublica import candidate_names, foundation_is_subjects


def test_entity_name_matching_is_strict():
    assert name_matches_entity("Timothy Cook", "COOK TIMOTHY D")
    assert name_matches_entity("Dorian Vexley-Marsh", "VEXLEY-MARSH DORIAN")
    assert name_matches_entity("Timothy Cook", "COOK TIMOTHY M")  # middle initials are not distinguishing; the issuer-anchor rule does that work
    assert not name_matches_entity("Timothy Cook", "COOKE TIMOTHY")
    assert not name_matches_entity("Timothy Cook", "COOK THOMAS")
    assert not name_matches_entity("Cook", "COOK TIMOTHY")


def test_parse_and_render_form4():
    xml = open("tests/fixtures/corpus/www.sec.gov/Archives/edgar/data/9100001/000091000026000001/form4.xml").read()
    p = parse_form4(xml)
    assert p["issuer"] == "Pelagic Systems, Inc." and p["ticker"] == "PLGS" and p["roles"] == ["director"]
    assert p["transactions"][0]["code"] == "S" and p["transactions"][0]["shares"] == "50000"
    text = render_form4(p, "2026-08-18", "https://example.sec/x")
    assert "On 2026-08-15, open-market sale of 50,000 shares of Pelagic Systems, Inc. Common Stock at $18.25 per share; 1,200,000 shares owned following the transaction (SEC Form 4 filed 2026-08-18)." in text


def test_foundation_matching_rules():
    assert foundation_is_subjects("Dorian and Imara Vexley-Marsh Foundation", "Dorian Vexley-Marsh", "", [])
    assert foundation_is_subjects("Imara Vexley-Marsh Fund", "Dorian Vexley-Marsh", "Imara Vexley-Marsh", [])
    assert not foundation_is_subjects("Marsh Family Foundation", "Dorian Vexley-Marsh", "", [])
    assert not foundation_is_subjects("Vexley-Marsh Family Foundation", "Dorian Vexley-Marsh", "", [])
    # a suffix of the hyphenated name is NOT the same foundation, even when the real one is mentioned in evidence
    assert not foundation_is_subjects("Marsh Family Foundation", "Dorian Vexley-Marsh", "Imara Vexley-Marsh", ["The Vexley-Marsh Family Foundation was established in 2016"])
    assert foundation_is_subjects("Vexley-Marsh Family Foundation", "Dorian Vexley-Marsh", "", ["The Vexley-Marsh Family Foundation was established in 2016"])
    assert candidate_names("Dorian Vexley-Marsh", ["Founded Halcyon Reef Capital", "Trustee of the Tidewater Trust"], "Imara Vexley-Marsh")[:2] == ["Vexley-Marsh foundation", "Dorian Vexley-Marsh foundation"]
