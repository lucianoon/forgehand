"""Independent checks copied into the sandbox only after the agent finishes."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from orders import line_total, total


def run_submitted_tests():
    # A timeout or spawn error must abort verification, not count as a mutation
    # caught by the regression suite. Discard bounded-run output to avoid
    # accumulating arbitrary test logs in the verifier process.
    return subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    ).returncode


case = sys.argv[1]
if case == "defect":
    # Discount once, preserving cents and the existing default and validation.
    for prices, discount, expected in [
        ([10, 30], 0.25, 30),
        ([10], 1, 0),
        ([], 0.5, 0),
        ([], 0, 0),
        ([], 1, 0),
        ([10], 0, 10),
        ([0, 0], 0.37, 0),
        ([1.25, 2.50, 0.25], 0.25, 3),
        ([19.99, 0.01], 0.1, 18),
        ([1.235], 0.1, 1.11),
        ([0.1, 0.2], 0.1, 0.27),
    ]:
        assert total(prices, discount) == expected, (prices, discount, expected)
    assert total([1.25, 2.5]) == 3.75
    # An empty order must not bypass the original invalid-discount check.
    for prices in [[], [10]]:
        for invalid in [
            -1,
            -0.001,
            1.001,
            2,
            float("nan"),
            float("inf"),
            -float("inf"),
        ]:
            try:
                total(prices, invalid)
            except ValueError:
                pass
            else:
                raise AssertionError(f"Invalid discount accepted: {invalid!r}")
    assert run_submitted_tests() == 0, "Submitted tests must pass before mutation"
    path = Path("orders.py")
    original = path.read_text()
    try:
        # Existing smoke tests pass when discounts are ignored. A requested
        # regression must fail if this original defect is reintroduced.
        path.write_text(
            original
            + "\n\ndef total(prices, discount=0):\n    return round(sum(prices), 2)\n"
        )
        assert run_submitted_tests() == 1, (
            "Submitted tests did not detect the ignored-discount mutation"
        )
    finally:
        path.write_text(original)
elif case == "tests":
    suite = unittest.defaultTestLoader.discover("tests")
    assert suite.countTestCases() >= 5, "Add at least three regression cases"
    assert unittest.TextTestRunner().run(suite).wasSuccessful()
    assert line_total(4, 0) == 0
    assert line_total(1.25, 3) == 3.75
    # Counting test names is not enough: each requested regression must catch
    # a corresponding behavior defect. Mutations exist only in this sandbox.
    path = Path("orders.py")
    original = path.read_text()
    mutations = [
        ("if quantity < 0:", "if False:"),
        ("return round(price * quantity, 2)", "return int(price * quantity)"),
        (
            "return round(price * quantity, 2)",
            "return round(price * max(quantity, 1), 2)",
        ),
    ]
    try:
        for before, after in mutations:
            assert before in original
            path.write_text(original.replace(before, after))
            assert run_submitted_tests() == 1, (
                "Regression tests did not catch an injected defect"
            )
    finally:
        path.write_text(original)
elif case == "configuration":
    assert json.loads(Path("config.json").read_text())["currency"] == "EUR"
    assert "EUR 12.00" in Path("README.md").read_text()
    assert (
        subprocess.check_output([sys.executable, "demo.py"], text=True).strip()
        == "EUR 12.00"
    )
else:
    raise AssertionError("unknown case")

# A zero exit alone does not prove imports and all assertions completed.
print("FORGEHAND_VERIFIER_OK")
