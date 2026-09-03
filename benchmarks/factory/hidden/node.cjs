const assert = require('node:assert/strict');
const fs = require('node:fs');
const catalog = require('./catalog.cjs');
if (process.argv[2] === 'feature') {
  assert.deepEqual(catalog.uniqueTags([' Foo ', 'foo', '', 'BAR', ' Bar ']), ['foo', 'bar']);
  assert.deepEqual(catalog.uniqueTags([]), []);
} else if (process.argv[2] === 'refactor') {
  for (const price of [0, 1, 1.29, 100, 12.34]) {
    assert.equal(catalog.retail(price), Math.round(price * 1.2 * 100) / 100);
    assert.equal(catalog.wholesale(price), Math.round(price * 1.1 * 100) / 100);
  }
  assert.equal((fs.readFileSync('catalog.cjs', 'utf8').match(/Math\.round/g) || []).length, 1);
} else throw Error('unknown case');
