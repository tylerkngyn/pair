"""The wire protocol. Add new discriminated event types here."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, field_validator

from app.contracts.game import (
    GameSyncCommand, GameStartCommand, GameStateEvent, GuessCommand,
    GuessResultEvent, RoundReadyCommand,
)
from app.strict_model import StrictModel


class JoinPayload(StrictModel):
    room: str
    handle: str = Field(default="", max_length=32)

    @field_validator("room")
    @classmethod
    def normalize_room(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 6 or not value.isascii() or not value.isalnum():
            raise ValueError("Room code must be 6 alphanumeric characters")
        return value

    @field_validator("handle")
    @classmethod
    def normalize_handle(cls, value: str) -> str:
        value = value.strip()
        if any(not character.isprintable() for character in value):
            raise ValueError("Display name must contain printable characters")
        return value


class ChatInput(StrictModel):
    text: str = Field(min_length=1, max_length=500)

    @field_validator("text")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class EmptyPayload(StrictModel):
    pass


class JoinEvent(StrictModel):
    type: Literal["join"]
    payload: JoinPayload


class ChatEvent(StrictModel):
    type: Literal["chat"]
    payload: ChatInput


class PingEvent(StrictModel):
    type: Literal["ping"]
    payload: EmptyPayload


class TypingInput(StrictModel):
    typing: bool


class TypingEvent(StrictModel):
    type: Literal["typing"]
    payload: TypingInput


REACTION_EMOJI = ("👍", "❤️", "😂", "😮", "👏")


class ReactionInput(StrictModel):
    emoji: Literal[REACTION_EMOJI]


class ReactionEvent(StrictModel):
    type: Literal["reaction"]
    payload: ReactionInput


# Game commands (GameStartCommand etc.) already carry type + request_id
# (and, for round_ready/guess, round_id) in a flat shape rather than the
# {"type", "payload"} envelope every other client event uses here — that
# flat shape is how app/contracts/game.py's own Command union discriminates
# internally. request_id/round_id/word all default-free, so the command
# model itself fits directly as `payload` below (its own `type` field just
# gets its default filled in from the inner JSON, which never repeats the
# outer "type" key on the wire). These thin wrappers are the minimal
# adaptation needed to fit the existing envelope convention.
class GameStartEvent(StrictModel):
    type: Literal["game_start"]
    payload: GameStartCommand


class RoundReadyEvent(StrictModel):
    type: Literal["round_ready"]
    payload: RoundReadyCommand


class GuessEvent(StrictModel):
    type: Literal["guess"]
    payload: GuessCommand


class GameSyncEvent(StrictModel):
    type: Literal["game_sync"]
    payload: GameSyncCommand


ClientEvent = Annotated[
    JoinEvent | ChatEvent | PingEvent | TypingEvent | ReactionEvent
    | GameStartEvent | RoundReadyEvent | GuessEvent | GameSyncEvent,
    Field(discriminator="type"),
]
client_event_adapter = TypeAdapter(ClientEvent)


class RoomStatePayload(StrictModel):
    room: str
    status: Literal["waiting", "paired"]
    peers: list[str]


class ChatPayload(StrictModel):
    sender: str
    text: str
    timestamp: int


class SystemPayload(StrictModel):
    text: str


class TypingPayload(StrictModel):
    sender: str
    typing: bool


class ReactionPayload(StrictModel):
    sender: str
    emoji: Literal[REACTION_EMOJI]


class ErrorPayload(StrictModel):
    code: Literal["ROOM_FULL", "INVALID_MSG"]
    message: str


class RoomStateEvent(StrictModel):
    type: Literal["room_state"] = "room_state"
    payload: RoomStatePayload


class ChatBroadcast(StrictModel):
    type: Literal["chat"] = "chat"
    payload: ChatPayload


class SystemEvent(StrictModel):
    type: Literal["system"] = "system"
    payload: SystemPayload


class ErrorEvent(StrictModel):
    type: Literal["error"] = "error"
    payload: ErrorPayload


class TypingBroadcast(StrictModel):
    type: Literal["typing"] = "typing"
    payload: TypingPayload


class ReactionBroadcast(StrictModel):
    type: Literal["reaction"] = "reaction"
    payload: ReactionPayload


ServerEvent = (
    RoomStateEvent | ChatBroadcast | SystemEvent | ErrorEvent
    | TypingBroadcast | ReactionBroadcast
    # GameStateEvent/GuessResultEvent (app/contracts/game.py) already use
    # the same {"type", "payload"} envelope as every event above, so they
    # are reused directly here rather than redefined.
    | GameStateEvent | GuessResultEvent
)
