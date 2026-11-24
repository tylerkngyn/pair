from app.practice.ranked import elo_update, k_factor, match_outcome, tier


def test_k_factor_provisional_vs_established():
    assert k_factor(0) == 32
    assert k_factor(19) == 32
    assert k_factor(20) == 16
    assert k_factor(500) == 16


def test_equal_ratings_win_loss_symmetric():
    new_a, new_b = elo_update(1000, 1000, 0, 0, 1.0)
    assert new_a == 1016
    assert new_b == 984


def test_equal_ratings_draw_unchanged():
    new_a, new_b = elo_update(1000, 1000, 0, 0, 0.5)
    assert new_a == 1000
    assert new_b == 1000


def test_established_player_moves_less_than_provisional():
    _, established_loss = elo_update(1000, 1000, 0, 20, 1.0)
    _, provisional_loss = elo_update(1000, 1000, 0, 0, 1.0)
    assert (1000 - established_loss) < (1000 - provisional_loss)


def test_underdog_win_gains_more_than_favorite_win():
    underdog_gain, _ = elo_update(900, 1100, 0, 0, 1.0)
    favorite_gain, _ = elo_update(1100, 900, 0, 0, 1.0)
    assert (underdog_gain - 900) > (favorite_gain - 1100)


def test_rating_floor_never_breached():
    new_a, new_b = elo_update(105, 105, 0, 0, 0.0)
    assert new_a == 100


def test_match_outcome_by_correctness():
    assert match_outcome(100.0, 50.0, 10, 10) == ("win", 1.0)
    assert match_outcome(50.0, 100.0, 10, 10) == ("loss", 0.0)


def test_match_outcome_tiebreak_by_elapsed_time():
    assert match_outcome(80.0, 80.0, 90, 120) == ("win", 1.0)
    assert match_outcome(80.0, 80.0, 120, 90) == ("loss", 0.0)


def test_match_outcome_full_draw():
    assert match_outcome(80.0, 80.0, 100, 100) == ("draw", 0.5)


def test_tier_bands():
    assert tier(100) == "Bronze"
    assert tier(499) == "Bronze"
    assert tier(500) == "Silver"
    assert tier(900) == "Gold"
    assert tier(5000) == "Diamond"
