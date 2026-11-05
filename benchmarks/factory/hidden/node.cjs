const assert = require('node:assert/strict');
const fs = require('node:fs');
const {spawnSync} = require('node:child_process');
const catalog = require('./catalog.cjs');
const caseName = process.argv[2];

function verifyPrices() {
  assert.equal(typeof catalog.retail, 'function');
  assert.equal(typeof catalog.wholesale, 'function');
  // Keep existing rounding and JavaScript numeric behavior; the original
  // functions do not reject negatives, non-finite values or coercible inputs.
  for (const price of [
    0, 1, 1.29, 100, 12.34, 0.005, 0.015, 2.675,
    -0.001, -1, -1.29, -12.34, NaN, Infinity, -Infinity,
    undefined, null, '12.34', '', true, false,
  ]) {
    assert.equal(catalog.retail(price), Math.round(price * 1.2 * 100) / 100,
      `retail changed for ${String(price)}`);
    assert.equal(catalog.wholesale(price), Math.round(price * 1.1 * 100) / 100,
      `wholesale changed for ${String(price)}`);
  }
}

function runSubmittedTests() {
  const completed = spawnSync(process.execPath, ['--test'], {
    timeout: 30000,
    killSignal: 'SIGKILL',
    stdio: 'ignore',
  });
  // Spawn failures, timeouts and signals do not prove a test caught a defect.
  if (completed.error) throw completed.error;
  assert.equal(completed.signal, null, 'Submitted test process was interrupted');
  assert.equal(typeof completed.status, 'number');
  return completed.status;
}

if (caseName === 'feature') {
  verifyPrices();
  for (const [input, expected] of [
    [[' Foo ', 'foo', '', 'BAR', ' Bar '], ['foo', 'bar']],
    [[], []],
    [[' \t\n', '\r\n', ' \u00a0 '], []],
    [[' Beta ', 'ALPHA', 'beta', ' gamma ', 'alpha', 'GAMMA', ' delta '],
      ['beta', 'alpha', 'gamma', 'delta']],
    [[' 42 ', '0', '42', ' 0 ', 'item-1'], ['42', '0', 'item-1']],
    [[' CAFÉ ', 'café', ' Coração ', 'CORAÇÃO'], ['café', 'coração']],
  ]) {
    assert.deepEqual(catalog.uniqueTags(input), expected);
  }
  assert.equal(runSubmittedTests(), 0, 'Submitted tests must pass before mutation');
  const original = fs.readFileSync('catalog.cjs', 'utf8');
  try {
    // Existing price tests still pass without normalization. Requested tests
    // must expose the defect when the new feature becomes an identity function.
    fs.writeFileSync('catalog.cjs', original + '\nmodule.exports.uniqueTags = tags => tags;\n');
    assert.equal(runSubmittedTests(), 1,
      'Submitted tests did not detect the tag-normalization mutation');
  } finally {
    fs.writeFileSync('catalog.cjs', original);
  }
} else if (caseName === 'refactor') {
  verifyPrices();
  assert.deepEqual(Object.keys(catalog).sort(), ['retail', 'wholesale'],
    'The shared helper must stay internal and the public API must stay unchanged');
  assert.equal((fs.readFileSync('catalog.cjs', 'utf8').match(/Math\.round/g) || []).length, 1);
} else throw Error('unknown case');

// An imported module may exit successfully before any assertion executes.
console.log('FORGEHAND_VERIFIER_OK');
