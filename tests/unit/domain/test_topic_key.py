import pytest

from nalar.domain.topic_key import topic_key


@pytest.mark.parametrize(
    ("title", "key"),
    [
        ("Gaya dan Gerak", "gaya-dan-gerak"),
        ("  Gaya   dan  Gerak! ", "gaya-dan-gerak"),
        ("Zat & Perubahannya (Bab 2)", "zat-perubahannya-bab-2"),
        ("Énergi Kalor", "energi-kalor"),
    ],
)
def test_topic_key_is_a_stable_slug(title: str, key: str) -> None:
    assert topic_key(title) == key


def test_a_title_without_letters_or_digits_is_rejected() -> None:
    with pytest.raises(ValueError):
        topic_key("!!!")
