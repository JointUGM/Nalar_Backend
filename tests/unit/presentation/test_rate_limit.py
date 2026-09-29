from nalar.presentation.api.rate_limit import RateLimiter


def test_the_eleventh_join_in_a_minute_is_refused_then_allowed_again() -> None:
    now = [0.0]
    limiter = RateLimiter(per_minute=10, clock=lambda: now[0])
    assert all(limiter.allow("student") for _ in range(10))
    assert not limiter.allow("student")
    assert limiter.allow("someone-else")
    now[0] = 61.0
    assert limiter.allow("student")
