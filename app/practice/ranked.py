"""Pure Elo update for ranked matches. No I/O; callers persist the result."""

RATING_FLOOR = 100
PROVISIONAL_MATCHES = 20
PROVISIONAL_K = 32
ESTABLISHED_K = 16


def k_factor(matches_played: int) -> int:
    return PROVISIONAL_K if matches_played < PROVISIONAL_MATCHES else ESTABLISHED_K


def expected_score(rating_a: int, rating_b: int) -> float:
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def elo_update(rating_a: int, rating_b: int, matches_played_a: int, matches_played_b: int, score_a: float):
    """score_a is 1 for a win, 0 for a loss, 0.5 for a draw, from a's perspective."""
    if score_a not in (0, 0.5, 1):
        raise ValueError("score_a must be 0, 0.5, or 1")
    expected_a = expected_score(rating_a, rating_b)
    new_a = rating_a + k_factor(matches_played_a) * (score_a - expected_a)
    new_b = rating_b + k_factor(matches_played_b) * ((1 - score_a) - (1 - expected_a))
    return max(RATING_FLOOR, round(new_a)), max(RATING_FLOOR, round(new_b))


def tier(rating: int) -> str:
    bands = ["Bronze", "Silver", "Gold", "Diamond"]
    index = min((rating - RATING_FLOOR) // 400, len(bands) - 1)
    return bands[max(0, index)]


def match_outcome(correctness_a: float, correctness_b: float, elapsed_a: float, elapsed_b: float):
    """Returns ("win"|"loss"|"draw", score_a) from a's perspective."""
    if correctness_a != correctness_b:
        return ("win", 1.0) if correctness_a > correctness_b else ("loss", 0.0)
    if elapsed_a != elapsed_b:
        return ("win", 1.0) if elapsed_a < elapsed_b else ("loss", 0.0)
    return ("draw", 0.5)
