from pydantic import BaseModel

class AuthPayload(BaseModel):
    email: str
    password: str
    name: str = ""
    # Required at registration (phone-linked account created alongside the
    # login, see db.register_user); ignored for login since the payload
    # shape is shared between both endpoints.
    phone_number: str = ""