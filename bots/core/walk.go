// SPDX-License-Identifier: Apache-2.0

package core

import "math"

const (
	// TicksPerSecond is the rate at which a bot sends movement input.
	TicksPerSecond = 20
	// WalkSpeed is the distance a walking bot covers per tick, in blocks.
	WalkSpeed = 0.2158
	// TurnRate is the yaw change per tick, in radians. A full circle takes 2π/TurnRate ≈ 628 ticks and has a
	// radius of WalkSpeed/TurnRate ≈ 21.6 blocks.
	TurnRate = 0.01
	// GoldenAngle spreads the start yaw of consecutive bots, in degrees.
	GoldenAngle = 137.508
)

// StartYaw is the initial yaw of the bot with the given index, in radians.
func StartYaw(index int) float64 {
	return math.Mod(float64(index)*GoldenAngle, 360) * math.Pi / 180
}

// Walker produces per-tick movement. The y coordinate never changes; servers that validate movement apply gravity.
type Walker struct {
	yaw    float64
	moving bool
}

// NewWalker returns the walker for a bot. mode is WalkCircle or WalkNone.
func NewWalker(mode string, index int) *Walker {
	return &Walker{yaw: StartYaw(index), moving: mode == WalkCircle}
}

// Moving reports whether the walker moves at all.
func (w *Walker) Moving() bool { return w.moving }

// Step advances one tick and returns the horizontal displacement in blocks.
func (w *Walker) Step() (dx, dz float64) {
	if !w.moving {
		return 0, 0
	}
	w.yaw = math.Mod(w.yaw+TurnRate, 2*math.Pi)
	return -math.Sin(w.yaw) * WalkSpeed, math.Cos(w.yaw) * WalkSpeed
}

// YawDegrees is the current yaw in degrees in [-180, 180), the range a game client reports.
func (w *Walker) YawDegrees() float64 {
	d := math.Mod(w.yaw*180/math.Pi+180, 360)
	if d < 0 {
		d += 360
	}
	return d - 180
}
