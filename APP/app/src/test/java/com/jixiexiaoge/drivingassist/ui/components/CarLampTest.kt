package com.jixiexiaoge.drivingassist.ui.components

import android.opengl.GLES20
import io.mockk.every
import io.mockk.mockkStatic
import io.mockk.unmockkStatic
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CarLampTest {
    @Test
    fun turnOverlayUsesTheSelectedSideAndCurrentBlinkPhase() {
        assertTrue(isTurnLampOverlayActive(true, 1, true, false, true))
        assertFalse(isTurnLampOverlayActive(true, 2, true, false, true))
        assertTrue(isTurnLampOverlayActive(true, 2, false, true, true))
        assertFalse(isTurnLampOverlayActive(true, 1, false, true, true))
        for (role in 1..2) {
            assertTrue(isTurnLampOverlayActive(true, role, true, true, true))
            assertFalse(isTurnLampOverlayActive(true, role, true, true, false))
            assertFalse(isTurnLampOverlayActive(true, role, null, null, true))
        }
    }

    @Test
    fun legacyModelsAndOtherRolesKeepTheirExistingEmissionPass() {
        for (role in 0..8) {
            assertFalse(usesTurnLampOverlay(false, role))
            assertFalse(isTurnLampOverlayActive(false, role, true, true, true))
            if (role !in 1..2) {
                assertFalse(usesTurnLampOverlay(true, role))
                assertFalse(isTurnLampOverlayActive(true, role, true, true, true))
            }
        }
    }

    @Test
    fun litOverlayPreservesDepthOcclusionAndRestoresBlendAndOffsetState() {
        verifyOverlayPass(throwDuringDraw = false)
    }

    @Test
    fun litOverlayRestoresStateWhenDrawingFails() {
        verifyOverlayPass(throwDuringDraw = true)
    }

    private fun verifyOverlayPass(throwDuringDraw: Boolean) {
        mockkStatic(GLES20::class)
        try {
            val enabled = mutableSetOf<Int>()
            val uniforms = mutableMapOf<Int, Float>()
            var depthWrite = true
            var depthFunction = 0
            var blend = 0 to 0
            var offset = 0f to 0f
            var depthDisabled = false
            var draws = 0
            every { GLES20.glEnable(any()) } answers { enabled.add(firstArg()); Unit }
            every { GLES20.glDisable(any()) } answers {
                if (firstArg<Int>() == GLES20.GL_DEPTH_TEST) depthDisabled = true
                enabled.remove(firstArg()); Unit
            }
            every { GLES20.glDepthMask(any()) } answers { depthWrite = firstArg() }
            every { GLES20.glDepthFunc(any()) } answers { depthFunction = firstArg() }
            every { GLES20.glBlendFunc(any(), any()) } answers { blend = firstArg<Int>() to secondArg<Int>() }
            every { GLES20.glPolygonOffset(any(), any()) } answers { offset = firstArg<Float>() to secondArg<Float>() }
            every { GLES20.glUniform1f(any(), any()) } answers { uniforms[firstArg()] = secondArg() }
            val result = runCatching {
                drawTurnLampOverlayPass(11, 12) {
                    draws++
                    assertTrue(GLES20.GL_DEPTH_TEST in enabled)
                    assertTrue(GLES20.GL_BLEND in enabled)
                    assertFalse(depthWrite)
                    assertEquals(GLES20.GL_LEQUAL, depthFunction)
                    assertEquals(GLES20.GL_SRC_ALPHA to GLES20.GL_ONE_MINUS_SRC_ALPHA, blend)
                    assertEquals(0f to -2f, offset)
                    assertEquals(1f, uniforms.getValue(11), 0f)
                    assertEquals(TURN_LAMP_NORMAL_OFFSET, uniforms.getValue(12), 0f)
                    if (throwDuringDraw) error("draw failure")
                }
            }
            assertEquals(throwDuringDraw, result.isFailure)
            if (!throwDuringDraw) result.getOrThrow()
            assertEquals(1, draws)
            assertFalse(depthDisabled)
            assertTrue(GLES20.GL_DEPTH_TEST in enabled)
            assertFalse(GLES20.GL_BLEND in enabled)
            assertFalse(GLES20.GL_POLYGON_OFFSET_FILL in enabled)
            assertTrue(depthWrite)
            assertEquals(0f to 0f, offset)
            assertEquals(0f, uniforms.getValue(11), 0f)
            assertEquals(0f, uniforms.getValue(12), 0f)
        } finally { unmockkStatic(GLES20::class) }
    }

    @Test
    fun authoredSharedTailLampBrightensOnlyForConfirmedBrakeTelemetry() {
        assertEquals(0.2f, tailLampIntensity(true, 7, false, false), 0f)
        assertEquals(0.2f, tailLampIntensity(true, 7, false, null), 0f)
        assertEquals(1f, tailLampIntensity(true, 7, false, true), 0f)
    }

    @Test
    fun authoredModelWithSeparateBrakeGeometryKeepsTailLampUnchanged() {
        for (brake in listOf(false, true, null)) {
            assertEquals(1f, tailLampIntensity(true, 7, true, brake), 0f)
        }
    }

    @Test
    fun legacyModelKeepsExistingTailLampBehavior() {
        for (separate in listOf(false, true)) for (brake in listOf(false, true, null)) {
            assertEquals(1f, tailLampIntensity(false, 7, separate, brake), 0f)
        }
    }

    @Test
    fun otherLampRolesNeverUseTheSharedTailLampIntensity() {
        for (role in (0..8).filter { it != 7 }) for (separate in listOf(false, true)) for (brake in listOf(false, true, null)) {
            assertEquals("role=$role separate=$separate brake=$brake", 1f,
                tailLampIntensity(true, role, separate, brake), 0f)
        }
    }
}
