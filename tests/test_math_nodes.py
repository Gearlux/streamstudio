"""Tests for FluxStudio scalar math expression nodes."""

import pytest

from fluxstudio.nodes import FloatMathNode, FloatToIntNode, IntMathNode, IntToFloatNode, _eval_formula

# ---------------------------------------------------------------------------
# _eval_formula
# ---------------------------------------------------------------------------


def test_eval_formula_addition() -> None:
    assert _eval_formula("a + b", 1.0, 2.0, 0.0, 0.0, 0.0) == pytest.approx(3.0)


def test_eval_formula_uses_all_five_variables() -> None:
    assert _eval_formula("a + b + c + d + e", 1, 2, 3, 4, 5) == pytest.approx(15)


def test_eval_formula_uses_math_sqrt() -> None:
    result = _eval_formula("sqrt(a)", 4.0, 0.0, 0.0, 0.0, 0.0)
    assert result == pytest.approx(2.0)


def test_eval_formula_uses_min_builtin() -> None:
    assert _eval_formula("min(a, b)", 3.0, 7.0, 0.0, 0.0, 0.0) == pytest.approx(3.0)


def test_eval_formula_uses_abs_builtin() -> None:
    assert _eval_formula("abs(a)", -5.0, 0.0, 0.0, 0.0, 0.0) == pytest.approx(5.0)


def test_eval_formula_raises_value_error_on_bad_formula() -> None:
    with pytest.raises(ValueError, match="Math formula"):
        _eval_formula("a / b", 1.0, 0.0, 0.0, 0.0, 0.0)  # ZeroDivisionError wrapped


def test_eval_formula_raises_value_error_on_syntax_error() -> None:
    with pytest.raises(ValueError, match="Math formula"):
        _eval_formula("a + (", 1.0, 2.0, 0.0, 0.0, 0.0)


def test_eval_formula_no_builtins_import_blocked() -> None:
    with pytest.raises(ValueError):
        _eval_formula("__import__('os').listdir('.')", 0, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# FloatMathNode
# ---------------------------------------------------------------------------


def test_float_math_node_default_formula() -> None:
    node = FloatMathNode()
    (result,) = node.compute("a + b", 0.0, 0.0)
    assert result == pytest.approx(0.0)
    assert isinstance(result, float)


def test_float_math_node_multiplication() -> None:
    node = FloatMathNode()
    (result,) = node.compute("a * b + c", 3.0, 4.0, 1.0)
    assert result == pytest.approx(13.0)


def test_float_math_node_output_is_float() -> None:
    node = FloatMathNode()
    (result,) = node.compute("a + b", 1, 2)  # int inputs
    assert isinstance(result, float)


def test_float_math_node_all_inputs() -> None:
    node = FloatMathNode()
    (result,) = node.compute("a + b + c + d + e", 1.0, 2.0, 3.0, 4.0, 5.0)
    assert result == pytest.approx(15.0)


def test_float_math_node_sqrt_formula() -> None:
    node = FloatMathNode()
    (result,) = node.compute("sqrt(a**2 + b**2)", 3.0, 4.0)
    assert result == pytest.approx(5.0)


def test_float_math_node_bad_formula_raises() -> None:
    node = FloatMathNode()
    with pytest.raises(ValueError):
        node.compute("bad formula [", 1.0, 2.0)


# ---------------------------------------------------------------------------
# IntMathNode
# ---------------------------------------------------------------------------


def test_int_math_node_default_formula() -> None:
    node = IntMathNode()
    (result,) = node.compute("a + b", 0, 0)
    assert result == 0
    assert isinstance(result, int)


def test_int_math_node_floor_division() -> None:
    node = IntMathNode()
    (result,) = node.compute("a // b", 7, 2)
    assert result == 3


def test_int_math_node_output_is_int() -> None:
    node = IntMathNode()
    (result,) = node.compute("a + b", 3, 4)
    assert isinstance(result, int)


def test_int_math_node_all_inputs() -> None:
    node = IntMathNode()
    (result,) = node.compute("a + b + c + d + e", 1, 2, 3, 4, 5)
    assert result == 15


def test_int_math_node_truncates_float_result() -> None:
    node = IntMathNode()
    (result,) = node.compute("a / b", 7, 2)  # 3.5 → int(3.5) == 3
    assert result == 3


# ---------------------------------------------------------------------------
# FloatToIntNode
# ---------------------------------------------------------------------------


def test_float_to_int_round_mode() -> None:
    node = FloatToIntNode()
    (result,) = node.convert(2.7, "round")
    assert result == 3
    assert isinstance(result, int)


def test_float_to_int_floor_mode() -> None:
    node = FloatToIntNode()
    (result,) = node.convert(2.9, "floor")
    assert result == 2


def test_float_to_int_ceil_mode() -> None:
    node = FloatToIntNode()
    (result,) = node.convert(2.1, "ceil")
    assert result == 3


def test_float_to_int_negative_floor() -> None:
    node = FloatToIntNode()
    (result,) = node.convert(-2.1, "floor")
    assert result == -3


def test_float_to_int_negative_ceil() -> None:
    node = FloatToIntNode()
    (result,) = node.convert(-2.9, "ceil")
    assert result == -2


def test_float_to_int_default_mode_is_round() -> None:
    node = FloatToIntNode()
    (result,) = node.convert(1.5)
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# IntToFloatNode
# ---------------------------------------------------------------------------


def test_int_to_float_converts_to_float() -> None:
    node = IntToFloatNode()
    (result,) = node.convert(7)
    assert result == pytest.approx(7.0)
    assert isinstance(result, float)


def test_int_to_float_zero() -> None:
    node = IntToFloatNode()
    (result,) = node.convert(0)
    assert result == 0.0


def test_int_to_float_negative() -> None:
    node = IntToFloatNode()
    (result,) = node.convert(-42)
    assert result == pytest.approx(-42.0)


# ---------------------------------------------------------------------------
# Category / registration sanity
# ---------------------------------------------------------------------------


def test_math_nodes_have_math_category() -> None:
    from fluxstudio.nodes import _MATH_CATEGORY

    for cls in (FloatMathNode, IntMathNode, FloatToIntNode, IntToFloatNode):
        assert cls.CATEGORY == _MATH_CATEGORY


def test_math_nodes_registered_in_mappings() -> None:
    from fluxstudio.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

    assert "Taidal_FloatMath" in NODE_CLASS_MAPPINGS
    assert "Taidal_IntMath" in NODE_CLASS_MAPPINGS
    assert "Taidal_FloatToInt" in NODE_CLASS_MAPPINGS
    assert "Taidal_IntToFloat" in NODE_CLASS_MAPPINGS

    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_FloatMath"] == "Float Math"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_IntMath"] == "Int Math"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_FloatToInt"] == "Float → Int"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_IntToFloat"] == "Int → Float"
