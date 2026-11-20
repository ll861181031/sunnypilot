package com.jixiexiaoge.drivingassist.ui.components

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SceneCameraTest {
    @Test
    fun reverseKeepsTheWholeCarInsideTheVerticalFieldOfView() {
        val eye = FloatArray(3)
        val target = FloatArray(3)
        sceneCameraEye("reverse", 0f, eye)
        sceneCameraTarget("reverse", target)
        val fy = target[1] - eye[1]
        val fz = target[2] - eye[2]
        val length = kotlin.math.sqrt(fy * fy + fz * fz)
        val forwardY = fy / length
        val forwardZ = fz / length
        // Actual closed-car envelope: 4.8m long, 1.7m high, translated +2.6m in Z.
        for (y in listOf(0f, 1.7f)) for (z in listOf(0.2f, 5f)) {
            val dy = y - eye[1]
            val dz = z - eye[2]
            val depth = dy * forwardY + dz * forwardZ
            val up = dy * forwardZ - dz * forwardY
            val ndcY = up / (depth * kotlin.math.tan(Math.toRadians(23.0)))
            assertTrue("car corner y=$y z=$z clips at NDC y=$ndcY",
                depth > 2f && kotlin.math.abs(ndcY) < 0.9)
        }
    }

    @Test
    fun lightingUsesTheSameEyeAsEachGearView() {
        val eye = FloatArray(3)
        sceneCameraEye("drive", 0f, eye)
        assertArrayEquals(floatArrayOf(-0.8f, 4f, -8.5f), eye, 0.00001f)
        sceneCameraEye("park", 0f, eye)
        assertArrayEquals(floatArrayOf(-8.6f, 3.9f, -6.2f), eye, 0.00001f)
        sceneCameraEye("reverse", 0f, eye)
        assertArrayEquals(floatArrayOf(0f, 2.8f, -6f), eye, 0.00001f)
    }

    @Test
    fun reverseLooksForwardFromBehindTheTailWithoutTheOverheadAngle() {
        val eye = FloatArray(3)
        val target = FloatArray(3)
        for (speed in listOf(0f, 10f, 30f)) {
            sceneCameraEye("reverse", speed, eye)
            sceneCameraTarget("reverse", target)
            assertEquals(0f, eye[0], 0f)
            assertTrue("camera must stay behind the tail", eye[2] < 0.2f)
            assertTrue("aim forward toward the hood", target[2] >= 4f)
            val downAngle = Math.toDegrees(kotlin.math.atan2(
                (eye[1] - target[1]).toDouble(), (target[2] - eye[2]).toDouble()))
            assertTrue("rear view should not be overhead: $downAngle", downAngle in 7.0..16.0)
        }
    }

    @Test
    fun drivePullsBackWithSpeedWhileParkRemainsFixed() {
        val eye = FloatArray(3)
        sceneCameraEye("drive", 100f, eye)
        assertArrayEquals(floatArrayOf(-0.8f, 4.4f, -9.7f), eye, 0.00001f)
        sceneCameraEye("park", 100f, eye)
        assertArrayEquals(floatArrayOf(-8.6f, 3.9f, -6.2f), eye, 0.00001f)
        sceneCameraEye(null, 100f, eye)
        assertArrayEquals(floatArrayOf(-0.8f, 4.4f, -9.7f), eye, 0.00001f)
    }
}
