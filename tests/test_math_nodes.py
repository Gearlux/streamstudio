"""Tests for the FluxStudio MathNode scalar expression node."""

import pytest

from fluxstudio.nodes import MathNode, _eval_formula

# ---------------------------------------------------------------------------
# _eval_formula
# ---------------------------------------------------------------------------


def test_eval_formula_addition() -> None:
    assert _eval_formula("a + b", 1.0, 2.0, 0, 0, 0) == pytest.approx(3.0)


def test_eval_formula_uses_all_five_variables() -> None:
    assert _eval_formula("a + b + c + d + e", 1, 2, 3, 4, 5) == pytest.approx(15)


def test_eval_formula_uses_math_sqrt() -> None:
    assert _eval_formula("sqrt(a)", 4.0, 0, 0, 0, 0) == pytest.approx(2.0)


def test_eval_formula_uses_min_builtin() -> None:
    assert _eval_formula("min(a, b)", 3.0, 7.0, 0, 0, 0) == pytest.approx(3.0)


def test_eval_formula_uses_abs_builtin() -> None:
    assert _eval_formula("abs(a)", -5.0, 0, 0, 0, 0) == pytest.approx(5.0)


def test_eval_formula_variable_e_shadows_euler() -> None:
    # The user-supplied value for 'e' must shadow math.e (~2.718).
    assert _eval_formula("e", 0, 0, 0, 0, 99) == pytest.approx(99)


def test_eval_formula_raises_on_division_by_zero() -> None:
    with pytest.raises(ValueError, match="Math formula"):
        _eval_formula("a / b", 1.0, 0.0, 0, 0, 0)


def test_eval_formula_raises_on_syntax_error() -> None:
    with pytest.raises(ValueError, match="Math formula"):
        _eval_formula("a + (", 1.0, 2.0, 0, 0, 0)


def test_eval_formula_import_blocked() -> None:
    with pytest.raises(ValueError):
        _eval_formula("__import__('os').listdir('.')", 0, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# MathNode — three outputs (float, int, bool)
# ---------------------------------------------------------------------------


def test_math_node_returns_three_outputs() -> None:
    node = MathNode()
    result = node.compute("a + b", 1.0, 2.0)
    assert len(result) == 3


def test_math_node_float_output() -> None:
    node = MathNode()
    f, _, _ = node.compute("a + b", 1.5, 2.5)
    assert f == pytest.approx(4.0)
    assert isinstance(f, float)


def test_math_node_int_output() -> None:
    node = MathNode()
    _, i, _ = node.compute("a * b", 3, 4)
    assert i == 12
    assert isinstance(i, int)


def test_math_node_bool_output_true() -> None:
    node = MathNode()
    _, _, b = node.compute("a > b", 10, 5)
    assert b is True
    assert isinstance(b, bool)


def test_math_node_bool_output_false() -> None:
    node = MathNode()
    _, _, b = node.compute("a > b", 3, 5)
    assert b is False


def test_math_node_all_five_inputs() -> None:
    node = MathNode()
    f, _, _ = node.compute("a + b + c + d + e", 1.0, 2.0, 3.0, 4.0, 5.0)
    assert f == pytest.approx(15.0)


def test_math_node_unwired_inputs_default_to_zero() -> None:
    node = MathNode()
    # Pass None to simulate an unwired optional socket.
    f, i, b = node.compute("a + b + c + d + e", None, None, None, None, None)
    assert f == pytest.approx(0.0)
    assert i == 0
    assert b is False


def test_math_node_accepts_int_inputs() -> None:
    node = MathNode()
    f, i, _ = node.compute("a // b", 7, 2)
    assert i == 3
    assert f == pytest.approx(3.0)


def test_math_node_accepts_bool_inputs() -> None:
    # BOOLEAN values from ComfyUI are Python bools — valid operands.
    node = MathNode()
    f, i, _ = node.compute("a + b", True, True)
    assert f == pytest.approx(2.0)
    assert i == 2


def test_math_node_sqrt_formula() -> None:
    node = MathNode()
    f, i, _ = node.compute("sqrt(a**2 + b**2)", 3.0, 4.0)
    assert f == pytest.approx(5.0)
    assert i == 5


def test_math_node_bad_formula_raises() -> None:
    node = MathNode()
    with pytest.raises(ValueError):
        node.compute("bad formula [", 1.0, 2.0)


# ---------------------------------------------------------------------------
# Category / registration sanity
# ---------------------------------------------------------------------------


def test_math_node_has_math_category() -> None:
    from fluxstudio.nodes import _MATH_CATEGORY

    assert MathNode.CATEGORY == _MATH_CATEGORY


def test_math_node_registered_in_mappings() -> None:
    from fluxstudio.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

    assert NODE_CLASS_MAPPINGS["Taidal_Math"] is MathNode
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_Math"] == "Math"


def test_old_math_nodes_removed_from_mappings() -> None:
    from fluxstudio.nodes import NODE_CLASS_MAPPINGS

    for old_key in ("Taidal_FloatMath", "Taidal_IntMath", "Taidal_FloatToInt", "Taidal_IntToFloat"):
        assert old_key not in NODE_CLASS_MAPPINGS
