"""
Safe arithmetic calculator using AST tree walking and decimal.Decimal precision.

Supported operations (one example each):
  Addition:       calculate("3 + 2")                    -> 5.0
  Subtraction:    calculate("10 - 4.5")                 -> 5.5
  Multiplication: calculate("6 * 7")                    -> 42.0
  Division:       calculate("15 / 4")                   -> 3.75
  Parentheses:    calculate("(2 + 3) * 4")              -> 20.0
  Combined:       calculate("(150 * 2) + 99.99 - 10")   -> 389.99
"""

import ast
from decimal import Decimal, InvalidOperation

from llm.tools import to_openai_tool

_ALLOWED_NODE_TYPES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.USub,
    ast.UAdd,
)


def _eval(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise ValueError(f"Unsupported literal: {node.value!r}")
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.USub, ast.UAdd)):
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        operand = _eval(node.operand)
        return -operand if isinstance(node.op, ast.USub) else operand
    if isinstance(node, ast.BinOp):
        left = _eval(node.left)
        right = _eval(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("Division by zero is not allowed.")
            return left / right
        raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
    raise ValueError(f"Unsupported expression node: {type(node).__name__}")


def calculate(expression: str) -> float:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid expression syntax: {e}") from e

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            raise ValueError(
                f"Unsupported node '{type(node).__name__}' — only +, -, *, / and parentheses allowed."
            )

    try:
        result = _eval(tree)
    except InvalidOperation as e:
        raise ValueError(f"Decimal computation error: {e}") from e

    return float(result)


CALCULATOR_TOOL = to_openai_tool(
    name="calculate",
    description=(
        "Evaluate a plain arithmetic expression and return the exact result. "
        "Use for any addition, subtraction, multiplication, or division — "
        "never compute arithmetic inline. Supports parentheses and decimals."
    ),
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": (
                    "Arithmetic expression string, e.g. '(200 * 3) + 50 - 15'. "
                    "Only +, -, *, / and parentheses allowed."
                ),
            },
        },
        "required": ["expression"],
    },
)
