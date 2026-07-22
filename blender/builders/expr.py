"""Safe arithmetic-expression evaluator for custom-primitive specs.

The LLM's "generate anything" path emits primitive dimensions as expressions
over parameter/toggle ids ("seat_height + 0.02"). This evaluator accepts ONLY
numbers, known names, + - * /, unary minus, parentheses, and min/max/abs —
anything else (attributes, subscripts, arbitrary calls) is rejected, so LLM
output can never execute code. Mirrored 1:1 in frontend/src/expr.ts.
"""
from __future__ import annotations

import ast
import math
import operator
from typing import Mapping, Union

Number = Union[int, float]

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_FUNCS = {"min": min, "max": max, "abs": abs}


class ExprError(ValueError):
    pass


def _eval(node: ast.AST, env: Mapping[str, float]) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ExprError(f"Non-numeric constant: {node.value!r}")
    if isinstance(node, ast.Name):
        try:
            return float(env[node.id])
        except KeyError:
            raise ExprError(
                f"Unknown name {node.id!r} (known: {', '.join(sorted(env)) or '<none>'})"
            ) from None
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval(node.left, env), _eval(node.right, env)
        try:
            return _BIN_OPS[type(node.op)](left, right)
        except ZeroDivisionError:
            raise ExprError(f"Division by zero: {ast.dump(node)}") from None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else v
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS
        and not node.keywords
        and node.args
    ):
        return float(_FUNCS[node.func.id](*(_eval(a, env) for a in node.args)))
    raise ExprError(f"Disallowed expression element: {ast.dump(node)}")


def safe_eval(expr: Union[str, Number], env: Mapping[str, float]) -> float:
    """Evaluate a number or an expression string against ``env``."""
    if isinstance(expr, (int, float)) and not isinstance(expr, bool):
        if not math.isfinite(expr):
            raise ExprError(f"Non-finite number: {expr!r}")
        return float(expr)
    if not isinstance(expr, str):
        raise ExprError(f"Expected number or expression string, got {type(expr).__name__}")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"Invalid expression {expr!r}: {exc.msg}") from None
    result = _eval(tree.body, env)
    if not math.isfinite(result):
        raise ExprError(f"Expression {expr!r} is not finite")
    return result
