// SPDX-License-Identifier: GPL-2.0-only
// Standalone behavioural checks for the experimental S10 libcamera AF patch.
#include "s10_autofocus.h"
#include <cassert>
#include <iostream>

using AF = libcamera::S10Autofocus;
static constexpr uint64_t tick = 100000000;

int main()
{
	// A noisy single focus peak should converge within one fine-search step.
	for (int target : { 0, 17, 173, 301, 495, 511 }) {
		AF af;
		uint64_t time = 0;
		int actual = 120, moves = 0;
		af.reset(actual, AF::Continuous, time);
		for (int n = 0; n < 250 && af.state() == AF::Scanning; ++n) {
			double score = .003 + .03 * std::exp(-std::pow((actual - target) / 60., 2));
			score *= 1 + .01 * std::sin(n * 2.1);
			af.update({ score, 150, true }, time += tick, [&](int p) {
				assert(p >= 0 && p <= 511);
				actual = p; ++moves; return 0;
			});
		}
		assert(af.state() == AF::Focused);
		assert(std::abs(actual - target) <= 16 && moves <= 18);
	}
	// Flat/no-texture scenes must not claim successful AF, and restore focus.
	for (bool valid : { false, true }) {
		AF af;
		int actual = 173;
		uint64_t time = 0;
		af.reset(actual, AF::Continuous, time);
		for (int n = 0; n < 250 && af.state() == AF::Scanning; ++n)
			af.update({ .01, 100, valid }, time += tick,
				  [&](int p) { actual = p; return 0; });
		assert(af.state() == AF::Failed && actual == 173);
	}
	// Cancelling auto AF or entering manual mode must hold the current lens.
	AF af;
	uint64_t time = 0;
	int moves = 0;
	auto move = [&](int) { ++moves; return 0; };
	af.reset(120, AF::Auto, time);
	for (int n = 0; n < 20; ++n)
		af.update({ .01, 100, true }, time += tick, move);
	assert(af.state() == AF::Idle && moves == 0);
	af.trigger(false, time);
	for (int n = 0; n < 20; ++n)
		af.update({ .01, 100, true }, time += tick, move);
	assert(moves > 0);
	af.trigger(true, time);
	int before = moves;
	for (int n = 0; n < 20; ++n)
		af.update({ .01, 100, true }, time += tick, move);
	assert(af.state() == AF::Idle && moves == before);
	af.setMode(AF::Continuous, time);
	af.pause(0, time);
	af.update({ .01, 100, true }, time += 400 * tick, move);
	assert(moves == before && af.pauseState() == 2);
	af.pause(2, time);
	assert(af.pauseState() == 0);
	af.setMode(AF::Manual, time);
	af.update({ .01, 100, true }, time += tick, move);
	assert(af.state() == AF::Idle && moves == before);

	// Guard truncated frames, darkness and clipping; check exposure scaling.
	const unsigned int w = 128, h = 128, stride = w * 2 + 16;
	std::vector<uint8_t> raw(stride * h);
	auto frame = [&](int exposure, int base) {
		for (unsigned int y = 0; y < h; ++y)
			for (unsigned int x = 0; x < w; ++x) {
				int v = base + exposure * (80 + int(30 * std::sin(x * .2) * std::cos(y * .2)));
				raw[y * stride + x * 2] = v & 255;
				raw[y * stride + x * 2 + 1] = v >> 8;
			}
		return AF::measure(raw.data(), raw.size(), w, h, stride);
	};
	auto m1 = frame(1, 64), m2 = frame(2, 64);
	assert(m1.valid && m2.valid && m1.contrast > 0);
	assert(std::abs(m2.contrast / m1.contrast - 1) < .01);
	assert(!AF::measure(raw.data(), 100, w, h, stride).valid);
	assert(!frame(0, 64).valid);
	assert(!frame(0, 1023).valid);
	std::cout << "AF convergence, bounds, flat/dark fallback, cancel/manual/pause and raw metric checks passed\n";
}
