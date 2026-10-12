# SPDX-License-Identifier: Apache-2.0
"""Check relational claims against a matrix summary (``bbench report --claims claims/<name>.toml``).

A claim turns a published statement into an expression over the summary, for example::

    median('endstone-0.11.14', 'tps_window_steady') / median('bds-1.26.52.3', 'tps_window_steady') >= 1.8

Expressions are parsed with :mod:`ast` and interpreted node by node; nothing reaches ``eval``. Allowed:

- numbers, strings, ``True`` and ``False``
- ``+ - * /``, unary ``-`` and ``+``, ``not``, ``and``, ``or``
- comparisons ``< <= > >= == !=``, chained as in Python
- data functions over the ok runs of the claim's scenario; their arguments must be string literals:

  - ``median(arm, metric)``, ``low(arm, metric)``, ``high(arm, metric)``: median, lowest and highest run. ``metric``
    may name one key of a distribution metric, as in ``'view_t90_own_s.p90'``.
  - ``n_ok(arm)``: number of ok runs
  - ``ratio(a, b, metric)``: ``median(a, metric) / median(b, metric)``
  - ``above(a, b, metric)``, ``below(a, b, metric)``: every ok run of ``a`` is above (below) every ok run of ``b``
    and the separation passes the report's difference rule

- ``abs(x)``, ``min(x, y, ...)``, ``max(x, y, ...)``

Verdicts: ``pass`` and ``fail``; ``inconclusive`` when the claim does not apply (another machine, or its
``inconclusive_if`` holds) or the data cannot decide it (no runs of an arm, metric not measured, a censored value);
``error`` when the expression fails while evaluating, for example by comparing a string with a number.
"""

from __future__ import annotations

import ast
import operator
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from bedrock_bench.config import ConfigError
from bedrock_bench.model import Claim, ClaimSet
from bedrock_bench.report.aggregate import Sample, bounded, samples, separated

PASS, FAIL, INCONCLUSIVE, ERROR = "pass", "fail", "inconclusive", "error"
VERDICTS = (PASS, FAIL, INCONCLUSIVE, ERROR)


@dataclass(frozen=True)
class _Signature:
    arity: int | None  # None: one or more arguments
    literal_args: int  # leading arguments that must be string literals


_FUNCTIONS: dict[str, _Signature] = {
    "median": _Signature(2, 2),
    "low": _Signature(2, 2),
    "high": _Signature(2, 2),
    "n_ok": _Signature(1, 1),
    "ratio": _Signature(3, 3),
    "above": _Signature(3, 3),
    "below": _Signature(3, 3),
    "abs": _Signature(1, 0),
    "min": _Signature(None, 0),
    "max": _Signature(None, 0),
}

_BINARY: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
}
_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


# --------------------------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------------------------


def parse_expression(text: str) -> ast.expr:
    """Parse a claim expression and check it against the whitelist.

    Raises ValueError naming the first construct that is not allowed.
    """
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"not a valid expression: {exc.msg}") from None
    _check(tree.body)
    return tree.body


def _check(node: ast.AST) -> None:
    match node:
        case ast.Constant(value=value) if isinstance(value, (int, float, str)):
            return
        case ast.UnaryOp(op=op, operand=operand) if type(op) in _UNARY:
            _check(operand)
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _BINARY:
            _check(left)
            _check(right)
        case ast.BoolOp(values=values):
            for value in values:
                _check(value)
        case ast.Compare(left=left, ops=ops, comparators=comparators) if all(type(o) in _COMPARE for o in ops):
            _check(left)
            for comparator in comparators:
                _check(comparator)
        case ast.Call(func=ast.Name(id=name), args=args, keywords=[]) if name in _FUNCTIONS:
            _check_call(name, args)
        case _:
            raise ValueError(f"{_describe(node)} is not allowed")


def _check_call(name: str, args: Sequence[ast.expr]) -> None:
    signature = _FUNCTIONS[name]
    if signature.arity is None and not args:
        raise ValueError(f"{name}() needs at least one argument")
    if signature.arity is not None and len(args) != signature.arity:
        raise ValueError(f"{name}() takes {signature.arity} arguments, got {len(args)}")
    for index, arg in enumerate(args):
        if index < signature.literal_args:
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                raise ValueError(f"argument {index + 1} of {name}() must be a string literal")
        else:
            _check(arg)


def _describe(node: ast.AST) -> str:
    match node:
        case ast.Name(id=name):
            return f"name {name!r}"
        case ast.Attribute(attr=attr):
            return f"attribute access .{attr}"
        case ast.Call(func=ast.Name(id=name), keywords=keywords) if name in _FUNCTIONS and keywords:
            return f"keyword arguments to {name}()"
        case ast.Call(func=ast.Name(id=name)):
            return f"function {name!r}"
        case ast.Call(func=func):
            return _describe(func)
        case ast.Constant(value=value):
            return f"constant {value!r}"
        case ast.BinOp(op=op) | ast.UnaryOp(op=op):
            return f"operator {type(op).__name__}"
        case ast.Compare(ops=ops):
            bad = next(o for o in ops if type(o) not in _COMPARE)
            return f"operator {type(bad).__name__}"
    return type(node).__name__


def check_claims(claim_set: ClaimSet) -> None:
    """Check every expression of a claims file; raises ConfigError naming the claim and key."""
    for index, claim in enumerate(claim_set.claims):
        for key in ("expr", "inconclusive_if"):
            text = getattr(claim, key)
            if text is None:
                continue
            try:
                parse_expression(text)
            except ValueError as exc:
                raise ConfigError(str(exc), file=claim_set.path, key=f"claim[{index}].{key}") from None


# --------------------------------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------------------------------


class _Undecided(Exception):
    """The data cannot decide the expression."""


class _Failed(Exception):
    """The expression is wrong for this data."""


class _Evaluator:
    def __init__(self, cells: Mapping[str, Mapping[str, Any]], scenario: str) -> None:
        self.cells = cells
        self.scenario = scenario
        self.values: dict[str, Any] = {}

    def run(self, text: str) -> tuple[str, Any]:
        """Evaluate ``text``: ``("value", v)``, ``("undecided", reason)`` or ``("error", message)``."""
        try:
            return "value", self.eval(parse_expression(text))
        except _Undecided as exc:
            return "undecided", str(exc)
        except (_Failed, TypeError, ValueError, ArithmeticError) as exc:
            return "error", str(exc)

    def eval(self, node: ast.expr) -> Any:
        match node:
            case ast.Constant(value=value):
                return value
            case ast.UnaryOp(op=op, operand=operand):
                return _UNARY[type(op)](self.eval(operand))
            case ast.BinOp(left=left, op=op, right=right):
                a, b = self.eval(left), self.eval(right)
                if isinstance(op, ast.Div) and b == 0:
                    raise _Undecided(f"division by zero in {ast.unparse(node)}")
                return _BINARY[type(op)](a, b)
            case ast.BoolOp(op=ast.And(), values=values):
                result: Any = True
                for value in values:
                    result = self.eval(value)
                    if not result:
                        return result
                return result
            case ast.BoolOp(values=values):
                result = False
                for value in values:
                    result = self.eval(value)
                    if result:
                        return result
                return result
            case ast.Compare(left=left, ops=ops, comparators=comparators):
                a = self.eval(left)
                for op, comparator in zip(ops, comparators, strict=True):
                    b = self.eval(comparator)
                    if not _COMPARE[type(op)](a, b):
                        return False
                    a = b
                return True
            case ast.Call(func=ast.Name(id=name), args=args):
                result = self.call(name, args)
                if name in _DATA_FUNCTIONS:
                    self.values[ast.unparse(node)] = result
                return result
        raise _Failed(f"{_describe(node)} is not allowed")

    def call(self, name: str, args: Sequence[ast.expr]) -> Any:
        if name in ("abs", "min", "max"):
            values = [self.eval(a) for a in args]
            return {"abs": abs, "min": min, "max": max}[name](*values)
        literals = [a.value for a in args if isinstance(a, ast.Constant)]
        if name == "n_ok":
            return int(self._cell(literals[0])["n_ok"])
        if name in ("median", "low", "high"):
            return self._stat(name, literals[0], literals[1])
        a, b, metric = literals
        if name == "ratio":
            denominator = self._stat("median", b, metric)
            if denominator == 0:
                raise _Undecided(f"median of {metric} for {b} is 0")
            return self._stat("median", a, metric) / denominator
        return separated(self._samples(a, metric), self._samples(b, metric), higher_is_better=name == "above")

    def _cell(self, arm: str) -> Mapping[str, Any]:
        cell = self.cells.get(arm)
        if cell is None:
            raise _Undecided(f"no runs of arm {arm!r} in scenario {self.scenario!r}")
        return cell

    def _samples(self, arm: str, metric: str) -> list[Sample]:
        cell = self._cell(arm)
        metric_id, _, part = metric.partition(".")
        agg = cell["metrics"].get(metric_id)
        if agg is None:
            raise _Undecided(f"{metric_id} is not recorded for {arm}")
        parts = agg.get("parts")
        if part:
            if not parts or part not in parts:
                raise _Failed(f"{metric_id} has no part {part!r}")
            stat = parts[part]
        elif parts:
            raise _Failed(f"{metric_id} is a distribution; name one of its parts, e.g. '{metric_id}.median'")
        else:
            stat = agg
        pairs = samples(stat)
        if not pairs:
            reasons = ", ".join(agg.get("reasons") or ()) or "no ok runs"
            raise _Undecided(f"{metric} is not measured for {arm} ({reasons})")
        return pairs

    def _stat(self, name: str, arm: str, metric: str) -> float:
        function = {"median": statistics.median, "low": min, "high": max}[name]
        value, censored = bounded(function, self._samples(arm, metric))
        if censored:
            raise _Undecided(f"{name}({arm!r}, {metric!r}) is censored (>{value:g})")
        return value


_DATA_FUNCTIONS = frozenset({"median", "low", "high", "n_ok", "ratio", "above", "below"})


def evaluate_claim(claim: Claim, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one claim; returns its verdict, the reason, and the value of every data function it called."""
    result: dict[str, Any] = {"id": claim.id, "text": claim.text, "expr": claim.expr}
    if claim.inconclusive_if:
        result["inconclusive_if"] = claim.inconclusive_if
    if claim.source:
        result["source"] = claim.source
    scenarios = {s["name"]: s for s in summary.get("scenarios", ())}
    name = claim.scenario
    if name is None:
        if len(scenarios) != 1:
            return {**result, "verdict": ERROR, "reason": "the matrix has several scenarios; the claim must name one"}
        name = next(iter(scenarios))
    result["scenario"] = name
    scenario = scenarios.get(name)
    if scenario is None:
        return {**result, "verdict": INCONCLUSIVE, "reason": f"scenario {name!r} has no runs in this matrix directory"}

    evaluator = _Evaluator({c["arm"]["id"]: c for c in scenario["cells"]}, name)
    gate = evaluator.run(claim.inconclusive_if) if claim.inconclusive_if else None
    main = evaluator.run(claim.expr)
    result["values"] = dict(sorted(evaluator.values.items()))
    machine = (scenario.get("machine") or {}).get("label")

    for outcome, what in ((gate, "inconclusive_if"), (main, "expr")):
        if outcome is not None and outcome[0] == "error":
            return {**result, "verdict": ERROR, "reason": f"{what}: {outcome[1]}"}
    if claim.machine is not None and machine != claim.machine:
        reason = f"the claim is for machine {claim.machine!r}; these runs are from {machine!r}"
        return {**result, "verdict": INCONCLUSIVE, "reason": reason}
    if gate is not None:
        if gate[0] == "undecided":
            return {**result, "verdict": INCONCLUSIVE, "reason": f"inconclusive_if: {gate[1]}"}
        if not isinstance(gate[1], bool):
            return {**result, "verdict": ERROR, "reason": "inconclusive_if does not evaluate to true or false"}
        if gate[1]:
            return {**result, "verdict": INCONCLUSIVE, "reason": f"inconclusive_if holds: {claim.inconclusive_if}"}
    if main[0] == "undecided":
        return {**result, "verdict": INCONCLUSIVE, "reason": main[1]}
    if not isinstance(main[1], bool):
        return {**result, "verdict": ERROR, "reason": "expr does not evaluate to true or false"}
    return {**result, "verdict": PASS if main[1] else FAIL, "reason": ""}


def evaluate(claim_set: ClaimSet, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate a claims file against a summary.

    Raises ConfigError when an expression is not allowed or the file is for another matrix.
    """
    check_claims(claim_set)
    if claim_set.matrix != summary.get("matrix"):
        raise ConfigError(
            f"claims are for matrix {claim_set.matrix!r}, the report is for {summary.get('matrix')!r}",
            file=claim_set.path,
            key="matrix",
        )
    results = [evaluate_claim(claim, summary) for claim in claim_set.claims]
    return {
        "file": claim_set.path.name,
        "sha256": claim_set.sha256,
        "matrix": claim_set.matrix,
        "counts": {verdict: sum(r["verdict"] == verdict for r in results) for verdict in VERDICTS},
        "results": results,
    }


__all__ = [
    "ERROR",
    "FAIL",
    "INCONCLUSIVE",
    "PASS",
    "VERDICTS",
    "check_claims",
    "evaluate",
    "evaluate_claim",
    "parse_expression",
]
