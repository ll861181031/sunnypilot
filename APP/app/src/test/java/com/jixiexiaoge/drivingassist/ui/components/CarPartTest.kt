package com.jixiexiaoge.drivingassist.ui.components

import android.opengl.GLES20
import io.mockk.Runs
import io.mockk.every
import io.mockk.just
import io.mockk.mockkStatic
import io.mockk.unmockkStatic
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class CarPartTest {
    @Test
    fun sharedTextureUploadsOnlyOnceAndRebuildsAfterContextLoss() {
        var uploads = 0
        val cache = CarTextureCache { _, _ -> ++uploads }
        val sharedTable = CarTextureTable(listOf(byteArrayOf(1, 2, 3)))
        assertEquals(1, cache.get(sharedTable, 0))
        assertEquals(1, cache.get(sharedTable, 0))
        assertEquals(1, uploads)
        assertEquals(0, cache.get(sharedTable, -1))
        assertEquals(0, cache.get(sharedTable, 1))
        cache.forgetContext()
        assertEquals(2, cache.get(sharedTable, 0))
        assertEquals(2, uploads)
    }

    @Test
    fun failedTextureUploadFallsBackWithoutRetryingEveryFrame() {
        var uploads = 0
        val cache = CarTextureCache { _, _ -> uploads++; 0 }
        val table = CarTextureTable(listOf(byteArrayOf(1)))
        repeat(5) { assertEquals(0, cache.get(table, 0)) }
        assertEquals(1, uploads)
    }

    @Test
    fun environmentDrawClearsTextureUvAndAlphaState() {
        mockkStatic(GLES20::class)
        try {
            var uvEnabled = true
            var texture = 17
            var textured = 1f
            var alphaMode = 1
            var cutoff = 0f
            every { GLES20.glDisableVertexAttribArray(3) } answers { uvEnabled = false }
            every { GLES20.glVertexAttrib2f(3, 0f, 0f) } just Runs
            every { GLES20.glActiveTexture(GLES20.GL_TEXTURE0) } just Runs
            every { GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, any()) } answers { texture = secondArg() }
            every { GLES20.glUniform1f(4, any()) } answers { textured = secondArg() }
            every { GLES20.glUniform1i(5, any()) } answers { alphaMode = secondArg() }
            every { GLES20.glUniform1f(6, any()) } answers { cutoff = secondArg() }
            resetCarTextureState(3, 4, 5, 6)
            assertFalse(uvEnabled)
            assertEquals(0, texture)
            assertEquals(0f, textured, 0f)
            assertEquals(0, alphaMode)
            assertEquals(0.5f, cutoff, 0f)
        } finally { unmockkStatic(GLES20::class) }
    }

    @Test
    fun textureCacheReleasesSharedImageOnlyOnce() {
        mockkStatic(GLES20::class)
        try {
            val deleted = mutableListOf<Int>()
            every { GLES20.glDeleteTextures(any(), any<IntArray>(), any()) } answers {
                deleted.addAll(secondArg<IntArray>().toList())
            }
            val table = CarTextureTable(listOf(byteArrayOf(1)))
            val cache = CarTextureCache { _, _ -> 9 }
            repeat(3) { cache.get(table, 0) }
            cache.release()
            assertEquals(listOf(9), deleted)
            cache.release()
            assertEquals(1, deleted.size)
        } finally { unmockkStatic(GLES20::class) }
    }

    @Test
    fun carMaterialDoesNotReadThePreviousEnvironmentVertexBuffer() {
        mockkStatic(GLES20::class)
        try {
            val materialAttribute = 2
            // Environment meshes leave the material array enabled before drawing the car.
            val enabledArrays = mutableSetOf(materialAttribute)
            var material = floatArrayOf()
            var draws = 0
            every { GLES20.glGenBuffers(any(), any<IntArray>(), any()) } just Runs
            every { GLES20.glBindBuffer(any(), any()) } just Runs
            every { GLES20.glBufferData(any(), any(), any(), any()) } just Runs
            every { GLES20.glVertexAttribPointer(any(), any(), any(), any(), any(), any<Int>()) } just Runs
            every { GLES20.glEnableVertexAttribArray(any()) } answers { enabledArrays.add(firstArg()); Unit }
            every { GLES20.glDisableVertexAttribArray(any()) } answers { enabledArrays.remove(firstArg()); Unit }
            every { GLES20.glVertexAttrib2f(materialAttribute, any(), any()) } answers {
                material = floatArrayOf(secondArg(), thirdArg())
            }
            every { GLES20.glDrawElements(any(), any(), any(), any<Int>()) } answers {
                assertFalse("aMat must use the car material constant, not the environment VBO",
                    materialAttribute in enabledArrays)
                draws++
            }
            val data = CarData("Car_Paint", FloatArray(18), shortArrayOf(0, 1, 2))
            CarPart(data).draw(0, 1, materialAttribute)
            assertEquals(1, draws)
            assertEquals(data.spec, material[0], 0f)
            assertEquals(data.gloss, material[1], 0f)
        } finally {
            unmockkStatic(GLES20::class)
        }
    }
}
