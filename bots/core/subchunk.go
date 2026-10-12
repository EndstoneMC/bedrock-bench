// SPDX-License-Identifier: Apache-2.0

package core

// Dimension ids.
const (
	DimOverworld = 0
	DimNether    = 1
	DimEnd       = 2
)

// DimensionRange returns the lowest sub-chunk index and the number of sub-chunks of a dimension's column:
// overworld -4..19, nether 0..7, the end 0..15. Unknown dimensions use the overworld range.
func DimensionRange(dim int32) (lowest int8, count int) {
	switch dim {
	case DimNether:
		return 0, 8
	case DimEnd:
		return 0, 16
	default:
		return -4, 24
	}
}

// SubChunkYOffsets returns the y offsets a bot requests for one column: exactly limit sub-chunks from the bottom
// of the dimension, or the whole column when limit is negative (no limit) or larger than the column.
func SubChunkYOffsets(dim int32, limit int32) []int8 {
	lowest, count := DimensionRange(dim)
	if limit >= 0 && int(limit) < count {
		count = int(limit)
	}
	out := make([]int8, count)
	for i := range out {
		out[i] = lowest + int8(i)
	}
	return out
}
