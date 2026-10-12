// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/EndstoneMC/bedrock-bench/bots/core"
	"github.com/sandertv/gophertunnel/minecraft"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

// multiplayerAudience is the audience of the self-signed identity token in the login format PocketMine-MP expects.
const multiplayerAudience = "api://auth-minecraft-services/multiplayer"

// TestLoginRequestFormat checks the offline login as a server receives it against the format PocketMine-MP
// expects. It is the failing case behind bots/patches/gophertunnel-v1.57.1/0001-login-backport.patch: unpatched
// v1.57.1 sends the player UUID as "identity" with no audience, and IsEditorMode instead of the newer client data
// fields.
func TestLoginRequestFormat(t *testing.T) {
	logins := make(chan []byte, 1)
	srv := startFakeServer(t, minecraft.ListenConfig{
		PacketFunc: func(h packet.Header, payload []byte, _, _ net.Addr) {
			if h.PacketID != packet.IDLogin {
				return
			}
			select {
			case logins <- bytes.Clone(payload):
			default:
			}
		},
	})
	_, errOut, stdin, exit := startBot(t, "-addr", srv.addr(), "-n", "1", "-prefix", "p", "-spawn-timeout", "20s")

	var payload []byte
	select {
	case payload = <-logins:
	case <-time.After(20 * time.Second):
		t.Fatalf("no login received; stderr:\n%s", errOut.String())
	}
	select {
	case <-srv.conns:
	case <-time.After(20 * time.Second):
		t.Fatalf("bot did not spawn; stderr:\n%s", errOut.String())
	}
	quitBot(t, stdin, exit, errOut)

	var pk packet.Login
	pk.Marshal(protocol.NewReader(bytes.NewReader(payload), 0, false))
	if pk.ClientProtocol != 1001 {
		t.Errorf("client protocol = %d, want 1001", pk.ClientProtocol)
	}
	identity, client := decodeConnectionRequest(t, pk.ConnectionRequest)

	if identity["leguuid"] != core.BotUUID("bedrock-bench", "p00") {
		t.Errorf("identity token leguuid = %v, want the bot's UUID", identity["leguuid"])
	}
	if _, ok := identity["identity"]; ok {
		t.Error("identity token still carries the pre-v1.58.0 \"identity\" claim")
	}
	if !hasAudience(identity["aud"], multiplayerAudience) {
		t.Errorf("identity token aud = %v, want %q", identity["aud"], multiplayerAudience)
	}
	if identity["xname"] != "p00" || identity["xid"] != "" {
		t.Errorf("identity token xname = %v, xid = %v; want p00 and no XUID", identity["xname"], identity["xid"])
	}

	newFields := []string{"FilterProfanity", "ClientEditorConnectionIntent", "ClientIsEditorCapable", "ProfileHash"}
	for _, key := range newFields {
		if _, ok := client[key]; !ok {
			t.Errorf("client data lacks %s", key)
		}
	}
	if _, ok := client["IsEditorMode"]; ok {
		t.Error("client data still carries the pre-v1.58.0 IsEditorMode field")
	}
	if client["CompatibleWithClientSideChunkGen"] != false || client["MaxViewDistance"] != float64(core.MaxRadius) ||
		client["MemoryTier"] != 0.0 {
		t.Errorf("client data hardware fields = %v, %v, %v", client["CompatibleWithClientSideChunkGen"],
			client["MaxViewDistance"], client["MemoryTier"])
	}
}

// decodeConnectionRequest splits the connection request of a Login packet into the claims of the self-signed
// identity token and of the client data token. Signatures are not checked; the listener already accepted them.
func decodeConnectionRequest(t *testing.T, req []byte) (identity, client map[string]any) {
	t.Helper()
	r := bytes.NewReader(req)
	readBlock := func() []byte {
		var n int32
		if err := binary.Read(r, binary.LittleEndian, &n); err != nil || n < 0 {
			t.Fatalf("connection request: bad length %d: %v", n, err)
		}
		b := make([]byte, n)
		if _, err := io.ReadFull(r, b); err != nil {
			t.Fatalf("connection request: %v", err)
		}
		return b
	}
	var outer struct {
		AuthenticationType int    `json:"AuthenticationType"`
		Token              string `json:"Token"`
	}
	if err := json.Unmarshal(readBlock(), &outer); err != nil {
		t.Fatalf("connection request JSON: %v", err)
	}
	if outer.Token == "" {
		t.Fatal("connection request has no identity token; the bot sent a legacy chain login")
	}
	return jwtClaims(t, outer.Token), jwtClaims(t, string(readBlock()))
}

func jwtClaims(t *testing.T, token string) map[string]any {
	t.Helper()
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		t.Fatalf("not a JWT: %q", token)
	}
	data, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatalf("JWT payload: %v", err)
	}
	var claims map[string]any
	if err := json.Unmarshal(data, &claims); err != nil {
		t.Fatalf("JWT claims: %v", err)
	}
	return claims
}

// hasAudience reports whether a JWT aud claim, a string or a list of strings, contains want.
func hasAudience(aud any, want string) bool {
	switch v := aud.(type) {
	case string:
		return v == want
	case []any:
		for _, a := range v {
			if a == want {
				return true
			}
		}
	}
	return false
}
