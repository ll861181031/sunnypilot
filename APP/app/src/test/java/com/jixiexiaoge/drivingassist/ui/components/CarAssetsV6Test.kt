package com.jixiexiaoge.drivingassist.ui.components

import android.content.Context
import android.content.res.AssetManager
import android.util.Log
import io.mockk.every
import io.mockk.mockk
import io.mockk.mockkStatic
import io.mockk.unmockkStatic
import java.nio.ByteBuffer
import java.nio.ByteOrder
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class CarAssetsV6Test {
    private val assets = mockk<AssetManager>()
    private val context = mockk<Context>()

    @Before fun setup() {
        every { context.assets } returns assets
        mockkStatic(Log::class)
        every { Log.i(any(), any()) } returns 0
        every { Log.e(any(), any(), any()) } returns 0
        every { Log.e(any(), any()) } returns 0
    }

    @After fun teardown() { unmockkStatic(Log::class) }

    private fun load(bytes: ByteArray): List<CarData> {
        every { assets.open("car.bin") } answers { bytes.inputStream() }
        return CarAssets.load(context)
    }

    @Test fun `v6 accepts explicit headlamp geometry but rejects unknown lamp roles`() {
        val f = fixture()
        f.bytes[f.headers[0] + 19] = 8
        val groups = load(f.bytes)
        assertEquals(2, groups.size)
        assertEquals(8, groups.first().lampRole)
        assertTrue(groups.first().isLamp)
        f.bytes[f.headers[0] + 19] = 9
        assertTrue(load(f.bytes).isEmpty())
    }

    @Test fun `v6 preserves UV material values and authored smooth normals`() {
        val group = load(fixture().bytes).first()
        assertArrayEquals(floatArrayOf(-0.5f, 1.25f, 0.5f, 0f, 1f, 1f), group.uv, 0f)
        assertArrayEquals(floatArrayOf(0.2f, 0.4f, 0.6f, 0.78f), group.color, 0f)
        assertEquals(0.78f, group.alpha, 0f)
        assertEquals(0.7f, group.spec, 0f)
        assertEquals(0.8f, group.gloss, 0f)
        assertTrue(group.isGlass)
        assertTrue(group.isTransparent)
        assertEquals(1, group.material!!.alphaMode)
        assertEquals(0, group.material.textureIndex)
        assertEquals(1, group.rigKind)
        assertEquals(0.1f, group.verts[6], 0.00001f)
        assertEquals(76f / kotlin.math.sqrt(76f * 76f + 102f * 102f), group.verts[4], 0.00001f)
    }

    @Test fun `v6 shares one encoded image table and preserves layered source triangles`() {
        val groups = load(fixture().bytes)
        assertEquals(2, groups.size)
        assertSame(groups[0].textureTable, groups[1].textureTable)
        assertArrayEquals(byteArrayOf(1, 2, 3, 4), groups[0].textureTable!!.images.single())
        // Same-rig coincident source triangles can be different textured layers, not legacy duplicates.
        assertEquals(3, groups[0].indices.size)
        assertEquals(3, groups[1].indices.size)
    }

    @Test fun `v6 explicit flags distinguish lamp glass names from windows`() {
        val f = fixture()
        val b = ByteBuffer.wrap(f.bytes).order(ByteOrder.LITTLE_ENDIAN)
        b.putInt(f.headers[0] + 64, 0)
        b.putInt(f.headers[1] + 64, 4)
        b.putFloat(f.headers[1] + 68, 0.35f)
        val groups = load(f.bytes)
        assertFalse(groups[0].isGlass)
        assertFalse(groups[0].isTransparent)
        assertEquals(0, groups[0].material!!.alphaMode)
        assertEquals(2, groups[1].material!!.alphaMode)
        assertEquals(0.35f, groups[1].material!!.alphaCutoff, 0f)
    }

    @Test fun `v6 rejects counts that would truncate to unsigned short`() {
        for ((offset, value) in listOf(4 to 65536, 12 to 1, 8 to Int.MAX_VALUE, 8 to 4)) {
            val f = fixture()
            ByteBuffer.wrap(f.bytes).order(ByteOrder.LITTLE_ENDIAN).putInt(f.headers[0] + offset, value)
            assertTrue("offset=$offset value=$value", load(f.bytes).isEmpty())
        }
    }

    @Test fun `v6 rejects out of range index rather than drawing corrupted geometry`() {
        val f = fixture()
        ByteBuffer.wrap(f.bytes).order(ByteOrder.LITTLE_ENDIAN).putShort(f.indices[0], 3)
        assertTrue(load(f.bytes).isEmpty())
    }

    @Test fun `v6 rejects nonfinite or invalid material and UV values`() {
        for (offset in listOf(20, 32, 36, 48, 52, 56, 68, 72 + 27)) {
            val f = fixture()
            ByteBuffer.wrap(f.bytes).order(ByteOrder.LITTLE_ENDIAN).putFloat(f.headers[0] + offset, Float.NaN)
            assertTrue("offset=$offset", load(f.bytes).isEmpty())
        }
        for (flags in listOf(-1, 6, 8)) {
            val f = fixture()
            ByteBuffer.wrap(f.bytes).order(ByteOrder.LITTLE_ENDIAN).putInt(f.headers[0] + 64, flags)
            assertTrue(load(f.bytes).isEmpty())
        }
    }

    @Test fun `v6 rejects missing images bad references and trailing data`() {
        val f = fixture()
        assertTrue(load(f.bytes.copyOf(f.bytes.size - 1)).isEmpty())
        assertTrue(load(f.bytes + byteArrayOf(0)).isEmpty())
        for (index in listOf(-2, 1, Int.MAX_VALUE)) {
            val g = fixture()
            ByteBuffer.wrap(g.bytes).order(ByteOrder.LITTLE_ENDIAN).putInt(g.headers[0] + 60, index)
            assertTrue(load(g.bytes).isEmpty())
        }
        val g = fixture()
        ByteBuffer.wrap(g.bytes).order(ByteOrder.LITTLE_ENDIAN).putInt(g.textureLength, Int.MAX_VALUE)
        assertTrue(load(g.bytes).isEmpty())
    }

    private data class Fixture(val bytes: ByteArray, val headers: List<Int>, val indices: List<Int>, val textureLength: Int)

    private fun fixture(): Fixture {
        val b = ByteBuffer.allocate(1024).order(ByteOrder.LITTLE_ENDIAN)
        b.put("CARM".toByteArray()).putInt(6).putInt(2)
        listOf("glas", "inner_light_glass").forEach {
            val name = it.toByteArray(); b.putShort(name.size.toShort()).put(name)
        }
        val headers = mutableListOf<Int>()
        val indices = mutableListOf<Int>()
        repeat(2) { i ->
            headers.add(b.position())
            b.putInt(i).putInt(3).putInt(3).putInt(0)
            b.put(1).put(1).put(60).put(0)
            b.putFloat(1f).putFloat(0.5f).putFloat(1.5f).putFloat(10000f)
            floatArrayOf(0.2f, 0.4f, 0.6f, 0.78f).forEach { b.putFloat(it) }
            b.putFloat(0.7f).putFloat(0.8f).putInt(0).putInt(3).putFloat(0.5f)
            intArrayOf(0, 0, 0, 1000, 0, 0, 0, 1000, 0).forEach { b.putShort(it.toShort()) }
            repeat(3) { b.put(0).put(76).put(102) }
            floatArrayOf(-0.5f, 1.25f, 0.5f, 0f, 1f, 1f).forEach { b.putFloat(it) }
            indices.add(b.position()); b.putShort(0).putShort(1).putShort(2)
        }
        b.putInt(1)
        val lengthOffset = b.position()
        b.putInt(4).put(byteArrayOf(1, 2, 3, 4))
        return Fixture(b.array().copyOf(b.position()), headers, indices, lengthOffset)
    }
}
