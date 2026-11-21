"""The wire protocol. Add new discriminated event types here."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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


ClientEvent = Annotated[
    JoinEvent | ChatEvent | PingEvent | TypingEvent | ReactionEvent,
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
)
