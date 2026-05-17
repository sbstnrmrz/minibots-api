from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.sql import func
from app.database import Base


class Workflow(Base):
    __tablename__ = "workflows"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AgentConfig(Base):
    __tablename__ = "agent_configs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    agent_type = Column(String, nullable=False)
    system_prompt = Column(String, nullable=True)
    config_json = Column(JSONB, nullable=True)


class WorkflowAgent(Base):
    __tablename__ = "workflow_agents"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False)
    agent_config_id = Column(Integer, ForeignKey("agent_configs.id"), nullable=False)
    position = Column(Integer, nullable=False)


class AgentTool(Base):
    __tablename__ = "agent_tools"

    id = Column(Integer, primary_key=True, index=True)
    agent_config_id = Column(Integer, ForeignKey("agent_configs.id"), nullable=False)
    tool_name = Column(String, nullable=False)


class RagSource(Base):
    __tablename__ = "rag_sources"

    id = Column(Integer, primary_key=True, index=True)
    namespace = Column(String, nullable=False, unique=True)
    scope_type = Column(String, nullable=False)  # "bot", "workflow", "agent"
    scope_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    bot_type = Column(String, nullable=False, server_default="zen_coach")
    system_prompt = Column(String, nullable=True)
    spreadsheet_id = Column(String, nullable=True)
    documents_urls = Column(ARRAY(String), nullable=True)
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=True)


class Chat(Base):
    __tablename__ = "chats"

    id = Column(String, primary_key=True)  # client-supplied UUID
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    chat_id = Column(String, ForeignKey("chats.id"), nullable=True)
    role = Column(String, nullable=False)  # "user" or "model"
    content = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
