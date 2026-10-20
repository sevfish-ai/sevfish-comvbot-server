"""
Device group models
"""

from typing import List
from pydantic import BaseModel, Field


class GroupCreateRequest(BaseModel):
    """Create group request model"""

    name: str = Field(..., min_length=1, max_length=100, description="Group name")


class GroupInfo(BaseModel):
    """Device group information model"""

    id: int = Field(..., description="Group ID")
    name: str = Field(..., description="Group name")
    created_at: str = Field(default="", description="Creation time")
    member_count: int = Field(default=0, description="Number of member devices")
    robots: List[str] = Field(default_factory=list, description="Member robot IDs")


class GroupMemberRequest(BaseModel):
    """Add robot to group request model"""

    robot_id: str = Field(..., description="Robot ID to add")
