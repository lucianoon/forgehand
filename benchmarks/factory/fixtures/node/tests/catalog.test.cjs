const {test} = require('node:test');
const assert = require('node:assert/strict');
const {retail, wholesale} = require('../catalog.cjs');
test('prices include tax', () => {
  assert.equal(retail(10), 12);
  assert.equal(wholesale(10), 11);
});
