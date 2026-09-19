from arte_suave_mcp import waf


def test_leading_zero_bits_matches_site_lz():
    assert waf.leading_zero_bits("0000ffff") == 16
    assert waf.leading_zero_bits("00001fff") == 19
    assert waf.leading_zero_bits("ffff") == 0
    assert waf.leading_zero_bits("1abc") == 3
    assert waf.leading_zero_bits("7fff") == 1
    assert waf.leading_zero_bits("8000") == 0


def test_solve_produces_valid_nonce():
    import hashlib
    token, difficulty = "abc", 12
    n = waf.solve(token, difficulty)
    h = hashlib.sha256(f"{token}:{n}".encode()).hexdigest()
    assert waf.leading_zero_bits(h) >= difficulty
    # smallest: every earlier nonce must be below difficulty
    for k in range(n):
        hk = hashlib.sha256(f"{token}:{k}".encode()).hexdigest()
        assert waf.leading_zero_bits(hk) < difficulty


def test_parse_challenge():
    html = 'var T="deadbeef",TS="1700000000",D=16;'
    assert waf.parse_challenge(html) == ("deadbeef", "1700000000", 16)
    assert waf.parse_challenge("no params here") is None


def test_is_challenge():
    assert waf.is_challenge("<title>Checking your browser...</title>")
    assert not waf.is_challenge("<div class=member-nav>ok</div>")
