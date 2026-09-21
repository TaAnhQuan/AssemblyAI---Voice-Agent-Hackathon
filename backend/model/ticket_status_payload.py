from pydantic import BaseModel

class TicketStatusPayload(BaseModel):
    status: str
