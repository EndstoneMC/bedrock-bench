// SPDX-License-Identifier: Apache-2.0

package core

// Selftest writes one event of every kind with synthetic values, using the same types the fleet uses. The harness
// validates this output against schemas/bot-event.schema.json, so a field the schema does not know fails a test
// instead of a benchmark run.
func Selftest(em *Emitter, build BuildInfo, flags map[string]any, intervals Intervals) {
	if build.Patches == nil {
		build.Patches = []string{}
	}
	dim := int32(DimNether)
	pktID := uint32(174)
	name := "b00"
	p50, p99, mx := 3.1, 48.0, 61.2

	em.Emit(EvHello, Hello{Build: build, Flags: flags, IntervalsMS: intervals})
	em.Emit(EvDial, Dial{Bot: "b00", I: 0})
	em.Emit(EvSpawned, Spawned{Bot: "b00", I: 0, Session: 1, JoinMS: 812.5, Radius: 10, Dim: DimOverworld})
	em.Emit(EvJoinFailed, JoinFailed{
		Bot: "b01", I: 1, Stage: StageSpawn, Err: "context deadline exceeded", Session: 1, Attempt: 1, Final: true,
	})
	em.Emit(EvTP, TP{Bot: "b00", Pos: [3]float64{1280.5, 301.62, 1280.5}, Dim: DimOverworld, N: 1})
	em.Emit(EvFirstChunk, FirstChunk{Bot: "b00", DtMS: 1430})
	em.Emit(EvProgress, Progress{Rows: [][]any{
		{"b00", 112, 98, 1280.5, 301.62, 1280.5, DimOverworld, 3, 1},
		{"b01", 0, 0, nil, nil, nil, nil, 0, 0},
	}})
	em.Emit(EvStat, Stat{
		Online: 1, Joined: 1, Failed: 1, RxPkts: 12345, TxPkts: 2345, RxBytes: 9876543, TxBytes: 123456,
		Corrections: 3, LevelChunks: 130, SubChunks: 2400, Uniq: 112, InView: 98,
	})
	em.Emit(EvPkt, Pkt{
		Rx: map[string]int64{"LevelChunk": 130, "CorrectPlayerMovePrediction": 3, "MoveActorDelta": 12},
		Tx: map[string]int64{"PlayerAuthInput": 1200, "SubChunkRequest": 130},
	})
	em.Emit(EvRTT, RTT{N: 12, P50MS: &p50, P99MS: &p99, MaxMS: &mx})
	em.Emit(EvDied, Lifecycle{Bot: "b00", Session: 1})
	em.Emit(EvRespawned, Lifecycle{Bot: "b00", Session: 1})
	em.Emit(EvDim, Lifecycle{Bot: "b00", Dim: &dim, Session: 1})
	em.Emit(EvLeft, Lifecycle{Bot: "b00", Session: 1})
	em.Emit(EvRejoin, Lifecycle{Bot: "b00", Session: 2})
	em.Emit(EvDropped, Lifecycle{Bot: "b00", Session: 2, Err: "connection timed out"})
	em.Emit(EvKicked, Lifecycle{Bot: "b00", Session: 3, Reason: "disconnectionScreen.serverFull"})
	em.Emit(EvDecodeError, DecodeError{Bot: &name, PktID: &pktID, Err: "decode packet *packet.SubChunk: unexpected EOF", Total: 1})
	em.Emit(EvAllJoined, AllJoined{Joined: 1, Failed: 1})
	em.Emit(EvBotSummary, BotSummary{
		Bot: "b00", Uniq: 112, InView: 98, Chunks: 120, Corrections: 3, FirstSeenMS: []int64{1430, 0, 0, 12, 3, 40},
	})
	em.Emit(EvDone, nil)
}
