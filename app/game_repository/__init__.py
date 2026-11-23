"""Storage adapters implementing app.contracts.game.GameRepository."""

from app.game_repository.memory import InMemoryGameRepository, StubScoreProvider

__all__ = ["InMemoryGameRepository", "StubScoreProvider"]
