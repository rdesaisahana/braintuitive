"""Avatar shop tests.

Points are the reward loop, so the rules about spending them need to hold
exactly. The one that matters most is the split: buying deducts from a
*balance*, never from the lifetime total, because the lifetime total is what
drives levelling. A child who saves up for the dearest character and drops
two levels for buying it has been punished for using their reward.

Run:
    cd backend
    pytest tests/test_avatars.py -v
"""

from __future__ import annotations

import pytest

from services import avatars

# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


def test_every_key_is_unique() -> None:
    keys = [avatar.key for avatar in avatars.CATALOGUE]
    assert len(keys) == len(set(keys))


def test_there_are_starters_and_they_are_free() -> None:
    assert avatars.STARTERS, "a child would begin as nobody"
    assert all(avatar.price == 0 for avatar in avatars.STARTERS)


def test_a_new_child_has_a_real_choice_of_starter() -> None:
    assert len(avatars.STARTERS) >= 2, "one free character is no choice at all"


def test_every_character_has_a_name_and_a_sticker() -> None:
    """The design's characters are named, and drawn as stickers."""
    for avatar in avatars.CATALOGUE:
        assert avatar.name, avatar.key
        assert avatar.image.startswith("/stickers/"), avatar.key


def test_first_catalogue_characters_still_resolve() -> None:
    """Profiles saved before the stickers keep what they bought and wore."""
    assert avatars.get("fox_scholar").key == "riley"
    assert avatars.get("kid_girl").key == "luna"
    assert "riley" in avatars.owned(["fox_scholar"])
    assert avatars.select("dragon_master", ["dragon_master"]) == "ziggy"
    for old, new in avatars.LEGACY.items():
        assert new in avatars.BY_KEY, f"{old} maps to a character that does not exist"


def test_bought_avatars_all_cost_something() -> None:
    earned = [avatar for avatar in avatars.CATALOGUE if not avatar.is_starter]
    assert earned
    assert all(avatar.price > 0 for avatar in earned)


def test_prices_are_reachable() -> None:
    """An avatar nobody can afford motivates nobody.

    The cheapest should be a couple of good quizzes away; the dearest should
    be a term's work, not a lifetime's.
    """
    prices = sorted(avatar.price for avatar in avatars.CATALOGUE if avatar.price)
    assert prices[0] <= 2 * avatars.QUIZ_WORTH, "the first purchase is too far away"
    assert prices[-1] <= 30 * avatars.QUIZ_WORTH, "the last is out of reach"


def test_an_unknown_key_is_not_invented() -> None:
    assert avatars.get("wyvern_supreme") is None
    assert avatars.get("") is None


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_starters_are_owned_even_when_nothing_is_stored() -> None:
    """Computed rather than stored, so adding a starter later gives it to every
    existing child instead of only to new ones."""
    assert avatars.owned(None) == set(avatars.starter_keys())
    assert avatars.owned([]) == set(avatars.starter_keys())


def test_owning_something_bought_keeps_the_starters() -> None:
    assert "nova" in avatars.owned(["nova"])
    assert set(avatars.starter_keys()) <= avatars.owned(["nova"])


# --------------------------------------------------------------------------- #
# Balance
# --------------------------------------------------------------------------- #


def test_balance_is_earned_minus_spent() -> None:
    assert avatars.balance(1000, 300) == 700


def test_balance_never_goes_negative() -> None:
    """A price change could otherwise show a child a debt they never incurred."""
    assert avatars.balance(100, 500) == 0


# --------------------------------------------------------------------------- #
# Buying
# --------------------------------------------------------------------------- #


def test_buying_spends_points_and_grants_the_avatar() -> None:
    unlocked, spent = avatars.purchase("nova", [], total_points=500, points_spent=0)
    assert "nova" in unlocked
    assert spent == avatars.get("nova").price


def test_buying_does_not_touch_lifetime_points() -> None:
    """The whole reason for the earned/spent split.

    ``purchase`` returns only the new spend; it is given no way to change the
    lifetime total, so it cannot cost a child a level even by accident.
    """
    _, spent = avatars.purchase("nova", [], total_points=500, points_spent=0)
    assert avatars.balance(500, spent) == 500 - 300


def test_buying_twice_over_is_refused() -> None:
    with pytest.raises(avatars.AvatarError, match="already have"):
        avatars.purchase("nova", ["nova"], total_points=9999, points_spent=0)


def test_a_starter_cannot_be_bought() -> None:
    """It is already owned, so this is the same refusal by a different route."""
    with pytest.raises(avatars.AvatarError, match="already have"):
        avatars.purchase("sunny", [], total_points=9999, points_spent=0)


def test_buying_beyond_the_balance_is_refused_with_the_shortfall() -> None:
    """The message is read by a child, so it says how much further to go."""
    with pytest.raises(avatars.AvatarError, match="200 more to go"):
        avatars.purchase("nova", [], total_points=100, points_spent=0)


def test_already_spent_points_cannot_be_spent_again() -> None:
    with pytest.raises(avatars.AvatarError, match="more to go"):
        avatars.purchase("nova", [], total_points=400, points_spent=350)


def test_buying_something_that_does_not_exist_is_refused() -> None:
    with pytest.raises(avatars.AvatarError, match="does not exist"):
        avatars.purchase("wyvern_supreme", [], total_points=9999, points_spent=0)


def test_exactly_enough_is_enough() -> None:
    """Off-by-one on a price is a child told they cannot afford what they can."""
    price = avatars.get("nova").price
    unlocked, _ = avatars.purchase("nova", [], total_points=price, points_spent=0)
    assert "nova" in unlocked


# --------------------------------------------------------------------------- #
# Wearing
# --------------------------------------------------------------------------- #


def test_wearing_a_starter_always_works() -> None:
    assert avatars.select("luna", []) == "luna"


def test_wearing_something_unowned_is_refused() -> None:
    """An avatar that can be set by asking is not a reward."""
    with pytest.raises(avatars.AvatarError, match="not unlocked"):
        avatars.select("ziggy", [])


def test_wearing_something_that_does_not_exist_is_refused() -> None:
    with pytest.raises(avatars.AvatarError, match="does not exist"):
        avatars.select("wyvern_supreme", ["wyvern_supreme"])


# --------------------------------------------------------------------------- #
# The shop view
# --------------------------------------------------------------------------- #


def test_the_shop_marks_what_is_owned_and_affordable() -> None:
    dragon = avatars.get("ziggy")
    view = avatars.to_dict(dragon, [], total_points=100, spent=0)
    assert view["owned"] is False
    assert view["affordable"] is False
    assert view["price"] == dragon.price

    rich = avatars.to_dict(dragon, [], total_points=99999, spent=0)
    assert rich["affordable"] is True


def test_something_owned_always_reads_as_affordable() -> None:
    """It is already theirs; showing it as unaffordable would imply they could
    lose it."""
    view = avatars.to_dict(avatars.get("nova"), ["nova"], 0, 0)
    assert view["owned"] is True
    assert view["affordable"] is True
