import random

from nalar.domain.join_code import ALPHABET, LENGTH, generate, normalize


def test_generated_codes_use_the_unambiguous_alphabet() -> None:
    rng = random.Random(7)
    for _ in range(200):
        code = generate(rng)
        assert len(code) == LENGTH == 6
        assert set(code) <= set(ALPHABET)
    assert not set("01IO") & set(ALPHABET)


def test_normalize_accepts_case_and_spaces_and_rejects_bad_codes() -> None:
    assert normalize(" abc234 ") == "ABC234"
    assert normalize("ABC23") is None
    assert normalize("ABC230") is None
