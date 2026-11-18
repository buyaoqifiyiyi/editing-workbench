const assert = require('node:assert/strict');
const {observe} = require('../src/jianying_engine/runtime/scripts/native_preview_stability.cjs');

const state = {};
const oldFrame = Buffer.from([32, 80, 160, 255]);
const currentFrame = Buffer.from([255, 0, 0, 255]);
for (const time of [0, 100, 200]) assert.equal(observe(state, oldFrame, time, 750), false);
for (const time of [300, 400, 500, 1000]) assert.equal(observe(state, currentFrame, time, 750), false);
assert.equal(observe(state, currentFrame, 1050, 750), true);
assert.equal(observe(state, oldFrame, 1100, 750), false);

const sequence = {};
assert.equal(observe(sequence, currentFrame, 0, 0), false);
assert.equal(observe(sequence, currentFrame, 100, 0), false);
assert.equal(observe(sequence, currentFrame, 200, 0), true);
console.log('PASS still-frame stability duration and frame-change reset; sequence retains three-read policy');
