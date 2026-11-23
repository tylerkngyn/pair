"""Base model shared by app/models.py and app/contracts/game.py.

Split out so neither module has to import the other just to get this.
"""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
