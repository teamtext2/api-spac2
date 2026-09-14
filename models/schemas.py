from pydantic import BaseModel


class UserProfile(BaseModel):
    id: int = 0
    user_id: int = 0
    name: str = ""
    username: str = ""
    bio: str = ""
    email: str = ""
    status: str = "online"
    avatar: str = ""
    avatar_url: Optional[str] = ""
    google_id: str = ""
    is_login: bool = False


class PushSubscriptionPayload(BaseModel):
    username: str
    token: str
    endpoint: str
    p256dh: str
    auth: str


class PushUnsubscribePayload(BaseModel):
    username: str
    token: str
    endpoint: str


class RegisterDevicePayload(BaseModel):
    device_id: str
    endpoint: str
    p256dh: str
    auth: str


class LinkDevicePayload(BaseModel):
    device_id: str
    username: str
    token: str


class UnlinkDevicePayload(BaseModel):
    device_id: str



class FriendRequest(BaseModel):
    username: str
    friend_username: str


class MarkDeliveredRequest(BaseModel):
    recipient: str
    message_ids: list


class MarkReadRequest(BaseModel):
    username: str
    message_ids: list


class DeleteMessagesRequest(BaseModel):
    username: str
    message_ids: list


class DeleteConversationRequest(BaseModel):
    username: str
    friend_username: str


class CallRejectRequest(BaseModel):
    caller: str
    recipient: str
