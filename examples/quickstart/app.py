from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from nemogate import Guard


class ScriptedModel(BaseChatModel):
    """Offline stand-in: tries a forbidden write, then stops. No network, no key."""

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(isinstance(m, ToolMessage) for m in messages):
            reply = AIMessage(content="Done.")
        else:
            call = {
                "name": "write_file",
                "args": {"file_path": "/migrations/001_users.sql", "content": "create table ..."},
                "id": "call-1",
            }
            reply = AIMessage(content="", tool_calls=[call])
        return ChatResult(generations=[ChatGeneration(message=reply)])


model = ScriptedModel()  # swap in your own model here

guard = Guard.from_yaml("rules.yaml")
agent = create_deep_agent(
    model=model,
    middleware=guard.middleware(),  # the main agent
    subagents=[
        SubAgent(  # the default general-purpose subagent does not get the main list
            name="general-purpose",
            description="General-purpose agent for multi-step work.",
            system_prompt="Do the work you are given.",
            model=model,
            middleware=guard.middleware(),
        )
    ],
    checkpointer=InMemorySaver(),
)

out = agent.invoke(
    {"messages": [{"role": "user", "content": "Add a users table."}]},
    {"configurable": {"thread_id": "quickstart-1"}},
)
for message in out["messages"]:
    if isinstance(message, ToolMessage):
        print(f"{message.name} -> {message.status}: {message.content}")
