function retail(price) { return Math.round(price * 1.2 * 100) / 100; }
function wholesale(price) { return Math.round(price * 1.1 * 100) / 100; }
module.exports = { retail, wholesale };
