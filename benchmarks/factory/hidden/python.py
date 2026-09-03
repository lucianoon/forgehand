"""Independent checks copied into the sandbox only after the agent finishes."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from orders import line_total, total

case = sys.argv[1]
if case == "defect":
    assert total([10, 30], .25) == 30
    assert total([10], 1) == 0
    assert total([], .5) == 0
    assert total([10], 0) == 10
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
        ("return round(price * quantity, 2)", "return round(price * max(quantity, 1), 2)"),
    ]
    try:
        for before, after in mutations:
            assert before in original
            path.write_text(original.replace(before, after))
            completed = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"], capture_output=True, timeout=30)
            assert completed.returncode != 0, "Regression tests did not catch an injected defect"
    finally:
        path.write_text(original)
elif case == "configuration":
    assert json.loads(Path("config.json").read_text())["currency"] == "EUR"
    assert "EUR 12.00" in Path("README.md").read_text()
    assert subprocess.check_output([sys.executable, "demo.py"], text=True).strip() == "EUR 12.00"
else:
    raise AssertionError("unknown case")
