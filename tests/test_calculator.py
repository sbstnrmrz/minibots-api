import pytest

from app.tools.calculator import calculate


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("3 + 2", 5.0),
        ("10 - 4.5", 5.5),
        ("6 * 7", 42.0),
        ("15 / 4", 3.75),
        ("(2 + 3) * 4", 20.0),
        ("(150 * 2) + 99.99 - 10", 389.99),
        ("-5", -5.0),
        ("+7", 7.0),
        ("3 * -4", -12.0),
    ],
)
def test_calculator_happy(expr, expected):
    assert calculate(expr) == expected


def test_calculator_decimal_precision():
    # Naked floats would give 0.30000000000000004 here.
    assert calculate("0.1 + 0.2") == 0.3


def test_calculator_rejects_division_by_zero():
    with pytest.raises(ValueError, match="zero"):
        calculate("10 / 0")


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd').read()",
        "x = 5",
        "[i for i in range(3)]",
        "2 ** 10",  # power not in allowlist
        "5 % 2",    # modulo not in allowlist
        "abs(-3)",  # function call not in allowlist
    ],
)
def test_calculator_rejects_unsafe(expr):
    with pytest.raises(ValueError):
        calculate(expr)


def test_calculator_rejects_syntax_error():
    with pytest.raises(ValueError, match="syntax"):
        calculate("3 +")
