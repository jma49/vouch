from vouch_verifier.claims import Citation, extract_claims

ENTITIES = {"NVDA", "AMD"}


def test_tier1_citation():
    answer = "NVDA's RSI(14) is 62.3 [[r:a1b2#/rsi_14]] as of yesterday's close."
    ex = extract_claims(answer, ENTITIES)
    assert len(ex.claims) == 1
    c = ex.claims[0]
    assert c.tier == 1
    assert c.value == 62.3
    assert c.citation == Citation(receipt_id="a1b2", json_ptr="/rsi_14")
    assert answer[c.span[0] : c.span[1]] == "62.3"


def test_tier1_binds_nearest_preceding_number():
    answer = "RSI moved from 55.1 to 62.3 [[r:a1b2#/rsi_14]]."
    ex = extract_claims(answer, ENTITIES)
    tier1 = [c for c in ex.claims if c.tier == 1]
    assert len(tier1) == 1
    assert tier1[0].value == 62.3


def test_tier2_entity_and_metric_scan():
    ex = extract_claims("NVDA closed at 181.52 and its RSI is 62.3.", ENTITIES)
    assert len(ex.claims) == 2
    close, rsi = ex.claims
    assert (close.entity, close.metric, close.value) == ("NVDA", "close_price", 181.52)
    assert (rsi.entity, rsi.metric, rsi.value) == ("NVDA", "rsi_14", 62.3)
    assert all(c.tier == 2 for c in ex.claims)


def test_tier2_percentage_fallback_metric():
    ex = extract_claims("AMD is down 1.35% on the day.", ENTITIES)
    assert len(ex.claims) == 1
    c = ex.claims[0]
    assert (c.entity, c.metric, c.value, c.unit) == ("AMD", "change_pct", 1.35, "pct")


def test_tier2_thousands_separators():
    ex = extract_claims("AMD volume came in at 52,410,000 shares.", ENTITIES)
    assert len(ex.claims) == 1
    assert ex.claims[0].value == 52410000.0
    assert ex.claims[0].metric == "volume"


def test_tier1_number_not_reused_by_tier2():
    answer = "NVDA RSI is 62.3 [[r:a1b2#/rsi_14]]."
    ex = extract_claims(answer, ENTITIES)
    assert len(ex.claims) == 1
    assert ex.claims[0].tier == 1


def test_unresolved_span_reported_not_guessed():
    ex = extract_claims("The answer is 42.", ENTITIES)
    assert ex.claims == ()
    assert len(ex.unresolved) == 1
    assert ex.unresolved[0].value == 42.0


def test_citation_internals_not_extracted_as_numbers():
    ex = extract_claims("Volume was 1200 [[r:9f#/bars/1/volume]] for NVDA.", ENTITIES)
    assert len(ex.claims) == 1
    assert ex.claims[0].value == 1200.0


def test_entity_scoped_to_sentence():
    answer = "NVDA closed at 181.52. Meanwhile volume overall was 99 somewhere."
    ex = extract_claims(answer, ENTITIES)
    assert len(ex.claims) == 1
    assert ex.claims[0].value == 181.52
    assert len(ex.unresolved) == 1


def test_tier_share():
    answer = "NVDA RSI is 62.3 [[r:a1#/rsi_14]]. NVDA closed at 181.52. Answer is 42."
    ex = extract_claims(answer, ENTITIES)
    assert ex.tier_share(1) == 1 / 3
    assert ex.tier_share(2) == 1 / 3
