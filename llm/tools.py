"""Tool schema adapter — converts tool definitions to OpenAI function-calling format."""


def to_openai_tool(name: str, description: str, parameters: dict) -> dict:
    """Convert a tool definition to the standard OpenAI function-calling schema.

    Args:
        name: tool/function name.
        description: what the tool does — shown to the model.
        parameters: JSON Schema object describing the tool's arguments.

    Returns:
        An OpenAI-format tool dict, ready for `call_llm(..., tools=[...])`.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
