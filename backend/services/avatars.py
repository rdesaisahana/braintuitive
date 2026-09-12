"""The character catalogue, and what it costs to own one.

Every child starts with a free character when their profile is created, and
buys the rest with points earned from quizzes. That is the whole reward loop:
answer questions, earn points, choose someone new to be.

**Points are earned once and spent once, but the level never falls.**
``total_points`` is the lifetime record and is what drives levelling;
``points_spent`` tracks what has been redeemed. A child who saves up for the
dearest character must not drop from level 4 back to level 2 -- that would
punish them for using the reward, which is the opposite of a reward.

The characters are the five from the approved Rewards design -- Sunny, Luna,
Nova, Riley and Ziggy -- drawn as stickers. ``image`` is the sticker's path in
the web app's public folder, so swapping the artwork means changing a file,
not this module.

Prices are a ladder, not a paywall. A perfect ten-question quiz earns about
200 points, so the first purchase is a couple of good sessions away and the
last is a few weeks' work. An avatar nobody can afford motivates nobody.

**Characters from the first catalogue still resolve.** Profiles saved before
the stickers name keys like ``fox_scholar``; :data:`LEGACY` maps each to its
successor, so nobody loses what they bought or wore and nothing on disk has to
be rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass

# What a perfect ten-question quiz is worth, give or take: 10 correct (100)
# plus passing (25) plus perfect (25) plus a tier (50). Prices are set against
# this so "how many good quizzes is that?" has an answer a child can hold.
QUIZ_WORTH = 200


@dataclass(frozen=True)
class Avatar:
    """One character a child can be."""

    key: str
    name: str
    image: str
    price: int
    blurb: str

    @property
    def is_starter(self) -> bool:
        return self.price == 0

    @property
    def label(self) -> str:
        """Something to call this avatar in a sentence."""
        return self.name or "this character"


CATALOGUE: tuple[Avatar, ...] = (
    # -- starters: free, one worn from the moment the profile exists ------- #
    Avatar("sunny", "Sunny", "/stickers/avatar-sunny.webp", 0, "A happy pup, always ready to start."),
    Avatar("luna", "Luna", "/stickers/avatar-luna.webp", 0, "A curious cat who notices everything."),
    # -- earned: bought with points, cheapest first ----------------------- #
    Avatar("nova", "Nova", "/stickers/avatar-nova.webp", 300, "A calm panda who never rushes a hard question."),
    Avatar("biscuit", "Biscuit", "/stickers/avatar-biscuit.webp", 400, "A cheerful corgi in a lucky bandana."),
    Avatar("pip", "Pip", "/stickers/avatar-pip.webp", 500, "A penguin who waves at every right answer."),
    Avatar("riley", "Riley", "/stickers/avatar-riley.webp", 600, "A clever fox, always a step ahead."),
    Avatar("ellie", "Ellie", "/stickers/avatar-ellie.webp", 700, "An elephant who never forgets a lesson."),
    Avatar("hoot", "Hoot", "/stickers/avatar-hoot.webp", 800, "A bookish owl in big round glasses."),
    Avatar("maple", "Maple", "/stickers/avatar-maple.webp", 900, "A fox with a backpack, off exploring."),
    Avatar("ziggy", "Ziggy", "/stickers/avatar-ziggy.webp", 1000, "A bouncy bunny for a big finish."),
    Avatar("shelly", "Shelly", "/stickers/avatar-shelly.webp", 1200, "A turtle who wins by never giving up."),
    Avatar("snowy", "Snowy", "/stickers/avatar-snowy.webp", 1400, "A polar bear with a big warm heart."),
    Avatar("daisy", "Daisy", "/stickers/avatar-daisy.webp", 1600, "A gentle deer with a flower behind her ear."),
    Avatar("kiki", "Kiki", "/stickers/avatar-kiki.webp", 1800, "A koala who holds on through the tricky bits."),
    Avatar("nutmeg", "Nutmeg", "/stickers/avatar-nutmeg.webp", 2000, "A squirrel who saves up for the best things."),
)

#: The first catalogue's keys, and the character that replaced each. Read
#: through, never written back: a stored ``fox_scholar`` simply *is* Riley.
LEGACY: dict[str, str] = {
    "kid_boy": "sunny",
    "kid_girl": "luna",
    "owl_default": "sunny",
    "cat_curious": "luna",
    "fox_scholar": "riley",
    "panda_calm": "nova",
    "otter_engineer": "ziggy",
    "robot_helper": "nova",
    "raven_strategist": "riley",
    "unicorn_bright": "ziggy",
    "dragon_master": "ziggy",
}

BY_KEY: dict[str, Avatar] = {avatar.key: avatar for avatar in CATALOGUE}
STARTERS: tuple[Avatar, ...] = tuple(a for a in CATALOGUE if a.is_starter)
DEFAULT_AVATAR = STARTERS[0].key


class AvatarError(Exception):
    """Raised when an avatar cannot be bought or selected."""


def get(key: str) -> Avatar | None:
    """Look one up, following a first-catalogue key to its successor.

    Returns None for a key that is in neither.
    """
    key = (key or "").strip()
    return BY_KEY.get(LEGACY.get(key, key))


def starter_keys() -> list[str]:
    """The keys every child owns from the moment they have a profile."""
    return [avatar.key for avatar in STARTERS]


def balance(total_points: int, points_spent: int) -> int:
    """Points available to spend.

    Clamped at zero: a catalogue price change could otherwise leave a child
    who already spent showing a negative balance, which reads as a debt they
    have somehow incurred.
    """
    return max(0, total_points - points_spent)


def owned(unlocked: list[str] | None) -> set[str]:
    """Everything a child owns, starters always included.

    Starters are computed rather than stored, so adding one later gives it to
    every existing child instead of only to new ones.
    """
    return {LEGACY.get(key, key) for key in (unlocked or [])} | set(starter_keys())


def can_afford(avatar: Avatar, total_points: int, points_spent: int) -> bool:
    return balance(total_points, points_spent) >= avatar.price


def purchase(
    key: str,
    unlocked: list[str] | None,
    total_points: int,
    points_spent: int,
) -> tuple[list[str], int]:
    """Buy an avatar, returning the new ``(unlocked, points_spent)``.

    Pure: the caller decides how to persist the result. Every refusal raises
    with a message written for the child, because they are the one reading it.

    Raises:
        AvatarError: unknown key, already owned, or not enough points.
    """
    avatar = get(key)
    if avatar is None:
        raise AvatarError("That character does not exist.")

    already = owned(unlocked)
    if avatar.key in already:
        raise AvatarError(f"You already have {avatar.label}.")

    if not can_afford(avatar, total_points, points_spent):
        short = avatar.price - balance(total_points, points_spent)
        raise AvatarError(f"{avatar.label} costs {avatar.price} points -- {short} more to go.")

    return sorted(already | {avatar.key}), points_spent + avatar.price


def select(key: str, unlocked: list[str] | None) -> str:
    """Choose which owned avatar to wear.

    Raises:
        AvatarError: unknown, or not owned yet.
    """
    avatar = get(key)
    if avatar is None:
        raise AvatarError("That character does not exist.")
    if avatar.key not in owned(unlocked):
        raise AvatarError(f"You have not unlocked {avatar.label} yet.")
    return avatar.key


def to_dict(avatar: Avatar, unlocked: list[str] | None, total_points: int, spent: int) -> dict:
    """Catalogue entry as the app sees it, including whether it is affordable."""
    is_owned = avatar.key in owned(unlocked)
    return {
        "key": avatar.key,
        "name": avatar.name,
        "image": avatar.image,
        "price": avatar.price,
        "blurb": avatar.blurb,
        "is_starter": avatar.is_starter,
        "owned": is_owned,
        "affordable": is_owned or can_afford(avatar, total_points, spent),
    }


__all__ = [
    "CATALOGUE",
    "DEFAULT_AVATAR",
    "LEGACY",
    "QUIZ_WORTH",
    "STARTERS",
    "Avatar",
    "AvatarError",
    "balance",
    "can_afford",
    "get",
    "owned",
    "purchase",
    "select",
    "starter_keys",
    "to_dict",
]
