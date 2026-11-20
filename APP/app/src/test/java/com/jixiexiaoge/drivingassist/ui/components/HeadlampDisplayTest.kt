package com.jixiexiaoge.drivingassist.ui.components

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class HeadlampDisplayTest {
    @Test fun onlyAuthoredHeadlampsUseTheDefaultDisplayEffect() {
        assertTrue(isDisplayHeadlamp(true, 8))
        for (role in 0..9) {
            assertFalse(isDisplayHeadlamp(false, role))
            if (role != 8) assertFalse(isDisplayHeadlamp(true, role))
        }
    }

    @Test fun realLowOrHighBeamWinsAndAbsentTelemetryKeepsDefaultPresentationLit() {
        // No telemetry yet -> keep the user-requested default presentation light.
        assertTrue(headlampOn(null, null))
        assertTrue(headlampOn(null, false))
        assertTrue(headlampOn(false, null))
        // Real switch reported: follow it.
        assertTrue(headlampOn(true, false))
        assertTrue(headlampOn(false, true))
        assertTrue(headlampOn(true, true))
        assertFalse(headlampOn(false, false))
    }

    @Test fun twinBeamsLightTheRoadAheadAndFadeAtTheirBoundaries() {
        val vertices = headlampBeamVertices().toList().chunked(4)
        assertTrue("headlights must illuminate the road", vertices.isNotEmpty())
        assertEquals(0, vertices.size % 3)
        assertTrue(vertices.flatten().all { it.isFinite() })
        assertTrue(vertices.all { it[1] > 0.02f && it[1] < 0.06f })
        assertTrue("car front is +Z; no backward light", vertices.all { it[2] >= 2.2f })
        assertTrue("light must reach past the hood", vertices.maxOf { it[2] } > 12f)
        assertTrue(vertices.all { it[3] in 0f..1f })
        assertTrue(vertices.any { it[0] < -0.5f && it[3] > 0.5f })
        assertTrue(vertices.any { it[0] > 0.5f && it[3] > 0.5f })
        val far = vertices.maxOf { it[2] }
        assertTrue("far edge must fade out", vertices.filter { it[2] == far }.all { it[3] == 0f })
        assertTrue("beam should spread ahead", vertices.any { kotlin.math.abs(it[0]) > 2f })
        // The two lamps must have equal energy; a missing or mirrored rear beam breaks symmetry.
        val left = vertices.filter { it[0] < 0f }.sumOf { it[3].toDouble() }
        val right = vertices.filter { it[0] > 0f }.sumOf { it[3].toDouble() }
        assertEquals(left, right, 0.001)
    }
}
