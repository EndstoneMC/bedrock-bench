// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"log"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/df-mc/dragonfly/server/entity/effect"
	"github.com/df-mc/dragonfly/server/world"
)

func TestFields(t *testing.T) {
	cases := map[string][]string{
		"":                         nil,
		"  ":                       nil,
		"tp a07 1280 300 1280":     {"tp", "a07", "1280", "300", "1280"},
		"tp  a07\t1 2 3\r":         {"tp", "a07", "1", "2", "3"},
		`kick "a player" too slow`: {"kick", "a player", "too", "slow"},
		`effect "" clear`:          {"effect", "", "clear"},
		`tp a"b c"d 1 2 3`:         {"tp", "ab cd", "1", "2", "3"},
	}
	for line, want := range cases {
		got, err := fields(line)
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Errorf("fields(%q) = %q, %v; want %q", line, got, err, want)
		}
	}
	if _, err := fields(`kick "a07 bye`); err == nil {
		t.Error("an unterminated quote was accepted")
	}
}

func TestParseEffect(t *testing.T) {
	e, err := parseEffect(effect.Resistance, nil)
	if err != nil || e.Level() != 1 || e.Infinite() || e.Duration() != 30*time.Second || e.ParticlesHidden() {
		t.Errorf("default effect = %+v, %v", e, err)
	}
	e, err = parseEffect(effect.Resistance, []string{"infinite", "255", "true"})
	if err != nil || e.Level() != 255 || !e.Infinite() || !e.ParticlesHidden() {
		t.Errorf("infinite 255 true = %+v, %v", e, err)
	}
	e, err = parseEffect(effect.Saturation, []string{"10", "0", "false"})
	if err != nil || e.Level() != 1 || e.Duration() != 10*time.Second || e.ParticlesHidden() {
		t.Errorf("10 0 false = %+v, %v", e, err)
	}
	for _, args := range [][]string{{"0"}, {"soon"}, {"10", "-1"}, {"10", "x"}, {"10", "1", "maybe"}} {
		if _, err := parseEffect(effect.Resistance, args); err == nil {
			t.Errorf("%q accepted", args)
		}
	}
}

func TestVec3(t *testing.T) {
	v, err := vec3([]string{"1280.5", "300", "-12"})
	if err != nil || v[0] != 1280.5 || v[1] != 300 || v[2] != -12 {
		t.Errorf("vec3 = %v, %v", v, err)
	}
	if formatVec3(v) != "1280.5 300 -12" {
		t.Errorf("formatVec3 = %q", formatVec3(v))
	}
	for _, args := range [][]string{{"~", "0", "0"}, {"NaN", "0", "0"}, {"0", "Inf", "0"}} {
		if _, err := vec3(args); err == nil {
			t.Errorf("%q accepted", args)
		}
	}
}

func TestRoster(t *testing.T) {
	r := newRoster()
	first, second := new(world.EntityHandle), new(world.EntityHandle)
	r.add("Bob", first)
	r.add("a07", second)
	if h, ok := r.get("bob"); !ok || h != first {
		t.Error("lookup by name is not case-insensitive")
	}
	if got := r.names(); !reflect.DeepEqual(got, []string{"Bob", "a07"}) {
		t.Errorf("names = %q", got)
	}
	rejoined := new(world.EntityHandle)
	r.add("a07", rejoined)
	r.remove("a07", second) // the old session quits after the new one joined
	if h, ok := r.get("a07"); !ok || h != rejoined {
		t.Error("the quit of an old session removed the new one")
	}
	r.remove("a07", rejoined)
	if _, ok := r.get("a07"); ok {
		t.Error("a07 is still listed after it quit")
	}
}

func TestServerVersion(t *testing.T) {
	for v, want := range map[string]string{
		"v0.11.6-0.20260920163020-1793369709ce":      "1793369",
		"v0.0.0-20260920163020-1793369709ce":         "1793369",
		"v0.11.6-rc.1.0.20260920163020-1793369709ce": "1793369",
		"v0.11.5":      "0.11.5",
		"v0.12.0-rc.1": "0.12.0-rc.1",
	} {
		if got := serverVersion(v); got != want {
			t.Errorf("serverVersion(%q) = %q, want %q", v, got, want)
		}
	}
}

// captureLog collects what the console logs while f runs.
func captureLog(t *testing.T, f func()) string {
	t.Helper()
	var buf bytes.Buffer
	flags, out := log.Flags(), log.Writer()
	log.SetFlags(0)
	log.SetOutput(&buf)
	defer func() {
		log.SetFlags(flags)
		log.SetOutput(out)
	}()
	f()
	return buf.String()
}

func TestConsoleRejectsBadCommands(t *testing.T) {
	c := newConsole(nil, newRoster()) // every command below fails before it needs the server
	cases := map[string]string{
		"nope":                               "ERROR Unknown command: nope.",
		"tp a07 1 2":                         "ERROR Usage: tp <target> <x> <y> <z>",
		"tp a07 1 2 x":                       "ERROR Invalid coordinate: x",
		"tp ghost 1 2 3":                     "ERROR No targets matched: ghost",
		"tpdim a07 moon 0 64 0":              "ERROR Unknown dimension: moon",
		"tpdim @a nether 0 64 0":             "ERROR tpdim takes one player",
		"effect a07 haste":                   "ERROR Unknown effect: haste",
		"effect a07 clear now":               "ERROR Usage: effect",
		"effect a07 resistance forever":      "ERROR Invalid duration: forever",
		"gamemode flying a07":                "ERROR Unknown game mode: flying",
		"weather snow":                       "ERROR Unknown weather: snow",
		"fillnear a07 0 0 0 64 64 64 stone":  "ERROR Too many blocks",
		"fillnear a07 0 -1 0 1 -1 1 bedrock": "ERROR Unknown block: bedrock",
		"fillnear @a 0 -1 0 1 -1 1 stone":    "ERROR fillnear takes one player",
		`kick "a07`:                          "ERROR Syntax error: unterminated quote",
	}
	for line, want := range cases {
		got := captureLog(t, func() { c.exec(line) })
		if !strings.HasPrefix(got, want) {
			t.Errorf("%q logged %q, want a line starting with %q", line, got, want)
		}
	}
	if got := captureLog(t, func() { c.exec("   ") }); got != "" {
		t.Errorf("a blank line logged %q", got)
	}
}

func TestHelpListsEveryCommand(t *testing.T) {
	got := captureLog(t, func() { newConsole(nil, newRoster()).exec("help") })
	for name, cmd := range commands {
		if !strings.Contains(got, "INFO "+cmd.usage+"\n") {
			t.Errorf("help does not list %s", name)
		}
	}
}
