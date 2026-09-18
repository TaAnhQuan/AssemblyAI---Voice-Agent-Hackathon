from pydantic import BaseModel

class TicketPayload(BaseModel):
    user_email: str = ""
    subject: str
    description: str
    category: str
    priority: str
    escalateVoice: bool = False