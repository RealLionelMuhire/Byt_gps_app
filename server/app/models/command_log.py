"""Command log — audit trail of tracker commands and their replies (migration 053)."""

from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, Text

from app.core.database import Base


class CommandLog(Base):
    """One row per command sent (and its reply), or per reply that no
    command accounts for (command=NULL, outcome='unmatched').

    The reply text is stored exactly as the tracker sent it — this is the
    record to check when "did the tracker really say that?" comes up.
    See app/tcp_server.py's send_command/handle_command_response.
    """

    __tablename__ = "command_log"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    imei = Column(String(20), nullable=False)
    command = Column(String(200), nullable=True)
    server_flag = Column(BigInteger, nullable=True)
    sent_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sent_at = Column(DateTime, nullable=True, default=datetime.utcnow)
    reply = Column(Text, nullable=True)
    reply_server_flag = Column(BigInteger, nullable=True)
    reply_at = Column(DateTime, nullable=True)
    # pending | replied | replied_unverified | timeout | late_reply | unmatched
    outcome = Column(String(20), nullable=False, default="pending")
