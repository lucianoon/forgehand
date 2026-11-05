"""Run trusted fixture controls locally; no model, network or Docker is needed.

Only copies of the versioned fixtures execute. These controls verify that the
independent acceptance checks distinguish known good changes from defects; they
are not a sandbox for arbitrary generated or adversarial code.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


FACTORY = Path(__file__).resolve().parents[2] / "benchmarks" / "factory"
CASES = {case["id"]: case for case in json.loads((FACTORY / "cases.json").read_text())}
COMPLETION_MARKER = "FORGEHAND_VERIFIER_OK"
REGRESSION_TESTS = """import unittest
from orders import line_total

class Regression(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(line_total(4, 0), 0)

    def test_fraction(self):
        self.assertEqual(line_total(1.25, 3), 3.75)

    def test_negative(self):
        with self.assertRaises(ValueError):
            line_total(4, -1)
"""
TAG_FEATURE = """
module.exports.uniqueTags = tags => [...new Set(
  tags.map(tag => tag.trim().toLowerCase()).filter(Boolean)
)];
"""
DISCOUNT_TESTS = """import unittest
from orders import total

class DiscountRegression(unittest.TestCase):
    def test_partial_discount_applied_once(self):
        self.assertEqual(total([10, 30], 0.25), 30)
"""
TAG_TESTS = """const {test} = require('node:test');
const assert = require('node:assert/strict');
const {uniqueTags} = require('../catalog.cjs');
test('tags are normalized and keep their first order', () => {
  assert.deepEqual(uniqueTags([' Foo ', 'BAR', 'foo', ' ']), ['foo', 'bar']);
  assert.deepEqual(uniqueTags([]), []);
});
"""
PRICE_REFACTOR = """function tax(price, rate) {
  return Math.round(price * rate * 100) / 100;
}
module.exports = {
  retail: price => tax(price, 1.2),
  wholesale: price => tax(price, 1.1)
};
"""


def _snapshot(root):
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


@pytest.fixture(autouse=True)
def unchanged_trusted_fixtures():
    before = _snapshot(FACTORY / "fixtures")
    yield
    assert _snapshot(FACTORY / "fixtures") == before


def _copy_case(tmp_path, case_id):
    case = CASES[case_id]
    root = tmp_path / case_id
    shutil.copytree(
        FACTORY / "fixtures" / case["ecosystem"],
        root,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    extension = "py" if case["ecosystem"] == "python" else "cjs"
    shutil.copyfile(
        FACTORY / "hidden" / f"{case['ecosystem']}.{extension}",
        root / f"__forgehand_verify.{extension}",
    )
    return root


def _run_verifier(root, case_id):
    case = CASES[case_id]
    if case["ecosystem"] == "python":
        command = [sys.executable, "-B", "__forgehand_verify.py"]
    else:
        node = shutil.which("node")
        if node is None:
            pytest.skip("Node.js is required for the real Node verifier controls")
        command = [node, "__forgehand_verify.cjs"]
    before = _snapshot(root)
    result = subprocess.run(
        [*command, case["hidden_case"]],
        cwd=root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # The Python test-quality verifier injects mutations but must restore them,
    # including when a regression suite fails to detect a mutation.
    assert _snapshot(root) == before
    return result


def _accepted(result):
    lines = result.stdout.splitlines()
    return result.returncode == 0 and bool(lines) and lines[-1] == COMPLETION_MARKER


def _reference_fix(root, case_id):
    if case_id == "python-defect":
        path = root / "orders.py"
        path.write_text(
            path.read_text().replace(
                "round(sum(prices), 2)", "round(sum(prices) * (1 - discount), 2)"
            )
        )
        (root / "tests" / "test_discount.py").write_text(DISCOUNT_TESTS)
    elif case_id == "python-tests":
        (root / "tests" / "test_regression.py").write_text(REGRESSION_TESTS)
    elif case_id == "python-configuration":
        (root / "config.json").write_text('{"currency": "EUR"}\n')
        path = root / "README.md"
        path.write_text(path.read_text().replace("USD", "EUR"))
    elif case_id == "node-feature":
        path = root / "catalog.cjs"
        path.write_text(path.read_text() + TAG_FEATURE)
        (root / "tests" / "tags.test.cjs").write_text(TAG_TESTS)
    elif case_id == "node-refactor":
        (root / "catalog.cjs").write_text(PRICE_REFACTOR)
    else:
        raise AssertionError(f"No trusted reference for {case_id}")


@pytest.mark.parametrize("case_id", CASES)
def test_verifier_rejects_unchanged_fixture(tmp_path, case_id):
    result = _run_verifier(_copy_case(tmp_path, case_id), case_id)
    assert result.returncode != 0, result.stdout
    assert not _accepted(result)


@pytest.mark.parametrize("case_id", CASES)
def test_verifier_accepts_reference_fix(tmp_path, case_id):
    root = _copy_case(tmp_path, case_id)
    _reference_fix(root, case_id)
    result = _run_verifier(root, case_id)
    assert _accepted(result), result.stdout + result.stderr


@pytest.mark.parametrize(
    ("case_id", "relative_path", "before", "after"),
    [
        pytest.param(
            "python-defect",
            "orders.py",
            "if not 0 <= discount <= 1:",
            "if False:",
            id="discount-validation-removed",
        ),
        pytest.param(
            "python-defect",
            "orders.py",
            "    if not 0 <= discount <= 1:",
            "    if not prices:\n        return 0\n    if not 0 <= discount <= 1:",
            id="empty-order-bypasses-discount-validation",
        ),
        pytest.param(
            "python-defect",
            "orders.py",
            "round(sum(prices) * (1 - discount), 2)",
            "round(round(sum(prices), 2) * (1 - discount), 2)",
            id="discount-rounds-subtotal-prematurely",
        ),
        pytest.param(
            "python-defect",
            "orders.py",
            "round(sum(prices) * (1 - discount), 2)",
            "round(sum(prices) * (1 - discount))",
            id="discount-drops-cents",
        ),
        pytest.param(
            "node-feature",
            "catalog.cjs",
            TAG_FEATURE,
            TAG_FEATURE + "module.exports.retail = () => 0;\n",
            id="tags-feature-breaks-pricing",
        ),
        pytest.param(
            "node-feature",
            "catalog.cjs",
            TAG_FEATURE,
            "module.exports.uniqueTags = tags => [...new Set("
            "tags.filter(Boolean).map(tag => tag.trim().toLowerCase()))];\n",
            id="tags-keeps-whitespace-only-values",
        ),
        pytest.param(
            "node-feature",
            "catalog.cjs",
            TAG_FEATURE,
            "module.exports.uniqueTags = tags => tags.map(tag => "
            "tag.trim().toLowerCase()).filter(Boolean).filter("
            "(tag, index, all) => tag !== all[index - 1]);\n",
            id="tags-removes-only-adjacent-duplicates",
        ),
        pytest.param(
            "node-refactor",
            "catalog.cjs",
            "price * rate * 100",
            "Math.max(price, 0) * rate * 100",
            id="refactor-clamps-negative-prices",
        ),
        pytest.param(
            "node-refactor",
            "catalog.cjs",
            "};",
            "};\nmodule.exports.tax = tax;",
            id="refactor-exposes-internal-helper",
        ),
        pytest.param(
            "node-refactor",
            "catalog.cjs",
            "price * rate * 100",
            "(Number.isNaN(price) ? 0 : price) * rate * 100",
            id="refactor-changes-nan-result",
        ),
        pytest.param(
            "python-tests",
            "tests/test_regression.py",
            REGRESSION_TESTS,
            "import unittest\nclass EmptyTests(unittest.TestCase):\n"
            "    def test_zero(self): pass\n"
            "    def test_fraction(self): pass\n"
            "    def test_negative(self): pass\n",
            id="regression-names-without-assertions",
        ),
        pytest.param(
            "python-tests",
            "tests/test_regression.py",
            "        with self.assertRaises(ValueError):\n            line_total(4, -1)",
            "        self.assertTrue(True)",
            id="missing-negative-quantity-regression",
        ),
        pytest.param(
            "python-configuration",
            "README.md",
            "EUR 12.00",
            "USD 12.00",
            id="configuration-documentation-disagrees",
        ),
    ],
)
def test_verifier_rejects_semantic_mutation(
    tmp_path, case_id, relative_path, before, after
):
    root = _copy_case(tmp_path, case_id)
    _reference_fix(root, case_id)
    path = root / relative_path
    original = path.read_text()
    assert before in original
    path.write_text(original.replace(before, after))
    result = _run_verifier(root, case_id)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not _accepted(result)


@pytest.mark.parametrize(
    ("case_id", "relative_path", "early_exit"),
    [
        ("python-defect", "orders.py", "raise SystemExit(0)\n"),
        ("node-feature", "catalog.cjs", "process.exit(0);\n"),
    ],
)
def test_successful_early_exit_does_not_complete_verification(
    tmp_path, case_id, relative_path, early_exit
):
    root = _copy_case(tmp_path, case_id)
    (root / relative_path).write_text(early_exit)
    result = _run_verifier(root, case_id)
    assert result.returncode == 0
    assert not _accepted(result)


@pytest.mark.parametrize("case_id", ["python-defect", "node-feature"])
@pytest.mark.parametrize("test_quality", ["missing", "irrelevant", "failing"])
def test_feature_or_fix_requires_working_regression(tmp_path, case_id, test_quality):
    root = _copy_case(tmp_path, case_id)
    _reference_fix(root, case_id)
    if case_id == "python-defect":
        path = root / "tests" / "test_discount.py"
        replacement = (
            "import unittest\nclass NoRegression(unittest.TestCase):\n"
            "    def test_unrelated(self):\n"
            f"        self.assertEqual(1, {2 if test_quality == 'failing' else 1})\n"
        )
    else:
        path = root / "tests" / "tags.test.cjs"
        replacement = (
            "const {test} = require('node:test');\n"
            "const assert = require('node:assert/strict');\n"
            "test('unrelated', () => "
            f"assert.equal(1, {2 if test_quality == 'failing' else 1}));\n"
        )
    if test_quality == "missing":
        path.unlink()
    else:
        path.write_text(replacement)
    result = _run_verifier(root, case_id)
    assert not _accepted(result), result.stdout + result.stderr
