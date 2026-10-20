"""
WebSocket message models
"""
from enum import IntEnum
from typing import Optional, List
from pydantic import BaseModel, Field


class MessageType(IntEnum):
    """Message type enumeration (aligned with WeworkMessageBean.java)"""
    HEART_BEAT = 11   # WeworkMessageBean.HEART_BEAT, sent as bare {"type":11}
    SEND_MESSAGE = 1
    REPLY_MESSAGE = 2
    GET_MESSAGE = 3


class SocketType(IntEnum):
    """Socket message type enumeration"""
    MESSAGE_LIST = 2
    MESSAGE_CONFIRM = 3


class RoomType(IntEnum):
    """Room type enumeration"""
    SINGLE_CHAT = 1  # Single chat
    GROUP_CHAT = 2   # Group chat


class TextType(IntEnum):
    """Text type enumeration"""
    TEXT = 1
    IMAGE = 2
    FILE = 3


class WeworkMessage(BaseModel):
    """Aivbot message model"""

    # Message type
    type: Optional[int] = Field(default=None, description="Message type")

    # Room information
    roomType: Optional[int] = Field(default=None, description="Room type (1=single, 2=group)")

    # Sender/Receiver information
    receivedName: Optional[str] = Field(default=None, description="Sender name")
    groupName: Optional[str] = Field(None, description="Group name")
    groupRemark: Optional[str] = Field(None, description="Group remark")

    # Message content
    text: Optional[str] = Field(default=None, description="Message text")
    textType: Optional[int] = Field(default=1, description="Text type (1=text, 2=image)")

    # Additional fields
    messageId: Optional[str] = Field(default=None, description="Message ID")
    atMe: Optional[bool] = Field(default=False, description="Is @me")
    timestamp: Optional[int] = Field(default=None, description="Timestamp")

    class Config:
        use_enum_values = True


class WeworkMessageList(BaseModel):
    """Aivbot message list model"""

    socketType: int = Field(default=2, description="Socket type")
    messageList: List[WeworkMessage] = Field(default_factory=list, description="Message list")
    messageId: Optional[str] = Field(default=None, description="Message ID for confirmation")

    class Config:
        use_enum_values = True


class WebSocketMessage(BaseModel):
    """Generic WebSocket message wrapper"""

    type: Optional[int] = None
    socketType: Optional[int] = None
    messageList: Optional[List[WeworkMessage]] = None
    messageId: Optional[str] = None
    td: Optional[int] = None  # Timestamp from client

    class Config:
        use_enum_values = True