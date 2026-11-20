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
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

class CarAssetsTest {
    private val assets = mockk<AssetManager>()
    private val context = mockk<Context>()

    @Before
    fun setup() {
        every { context.assets } returns assets
        mockkStatic(Log::class)
        every { Log.i(any(), any()) } returns 0
    }

    @After
    fun teardown() {
        unmockkStatic(Log::class)
    }

    @Test
    fun unsignedShortIndicesRetainNormalsAcrossSignedBoundary() {
        assertTriangleNormals(indices32 = false)
    }

    @Test
    fun intAssetIndicesRetainNormalsWhenUploadedAsUnsignedShorts() {
        assertTriangleNormals(indices32 = true)
    }

    @Test
    fun readsExplicitPositionScaleFromV5Header() {
        val bytes = triangleAsset(0, false, version = 5, positionScale = 10000f)
        every { assets.open("car.bin") } answers { bytes.inputStream() }
        val group = CarAssets.load(context).single()
        assertEquals(0.1f, group.verts[6], 0.00001f)
        assertEquals(0.1f, group.verts[13], 0.00001f)
    }

    @Test
    fun absentOrInvalidV5ScaleRetainsLegacyMillimeterCoordinates() {
        for (scale in listOf(0f, -1f, Float.NaN, Float.POSITIVE_INFINITY)) {
            val bytes = triangleAsset(0, false, version = 5, positionScale = scale)
            every { assets.open("car.bin") } answers { bytes.inputStream() }
            val group = CarAssets.load(context).single()
            assertEquals(1f, group.verts[6], 0.00001f)
            assertEquals(1f, group.verts[13], 0.00001f)
        }
    }

    @Test
    fun submillimeterTrianglesKeepTheirGeometricNormals() {
        val bytes = triangleAsset(0, false, version = 5, positionScale = 10000f, edge = 1)
        val normalStart = bytes.size - 6 - 9
        repeat(9) { bytes[normalStart + it] = 0 }
        every { assets.open("car.bin") } answers { bytes.inputStream() }
        val group = CarAssets.load(context).single()
        for (v in 0..2) {
            assertEquals(0f, group.verts[v * 6 + 4], 0.00001f)
            assertEquals(1f, group.verts[v * 6 + 5], 0.00001f)
        }
    }

    @Test
    fun v5PreservesAuthoredSmoothNormalsInsteadOfFlatteningEachFace() {
        val bytes = triangleAsset(0, false, version = 5, positionScale = 10000f)
        val normalStart = bytes.size - 6 - 9
        // A smooth curved surface can have a corner normal distinct from its face normal.
        repeat(3) { vertex ->
            bytes[normalStart + vertex * 3] = 0
            bytes[normalStart + vertex * 3 + 1] = 76
            bytes[normalStart + vertex * 3 + 2] = 102
        }
        every { assets.open("car.bin") } answers { bytes.inputStream() }
        val group = CarAssets.load(context).single()
        val length = kotlin.math.sqrt(76f * 76f + 102f * 102f)
        for (vertex in 0..2) {
            assertEquals(76f / length, group.verts[vertex * 6 + 4], 0.00001f)
            assertEquals(102f / length, group.verts[vertex * 6 + 5], 0.00001f)
        }
    }

    @Test
    fun v5MissingNormalsFallBackToGeometryPerVertex() {
        val bytes = triangleAsset(0, false, version = 5, positionScale = 10000f)
        val normalStart = bytes.size - 6 - 9
        repeat(9) { bytes[normalStart + it] = 0 }
        // One authored smooth corner and two absent corners must be handled independently.
        bytes[normalStart + 1] = 76
        bytes[normalStart + 2] = 102
        every { assets.open("car.bin") } answers { bytes.inputStream() }
        val group = CarAssets.load(context).single()
        assertEquals(76f / kotlin.math.sqrt(76f * 76f + 102f * 102f), group.verts[4], 0.00001f)
        for (vertex in 1..2) {
            assertEquals(0f, group.verts[vertex * 6 + 4], 0.00001f)
            assertEquals(1f, group.verts[vertex * 6 + 5], 0.00001f)
        }
    }

    @Test
    fun explicitPartAssignmentsKeepBodyLampsOnBody() {
        val bytes = tailLampAsset(version = 5)
        every { assets.open("car.bin") } answers { bytes.inputStream() }
        val groups = CarAssets.load(context)
        assertEquals(2, groups.size)
        val lamp = groups.single { it.lampRole == 7 }
        assertEquals(0, lamp.rigKind)
        assertEquals(6, lamp.indices.size)
    }

    @Test
    fun legacyAssetsStillSplitTailBandOntoTrunk() {
        val bytes = tailLampAsset(version = 4)
        every { assets.open("car.bin") } answers { bytes.inputStream() }
        val groups = CarAssets.load(context)
        assertEquals(3, groups.size)
        assertEquals(3, groups.single { it.rigKind == 0 }.indices.size)
        assertEquals(3, groups.single { it.rigKind == 6 && it.lampRole == 7 }.indices.size)
    }

    @Test
    fun legacyFormatsDoNotInterpretV6HeadlampRole() {
        for (version in listOf(4, 5)) for (role in listOf(7, 8)) {
            val bytes = triangleAsset(0, false, version = version)
            val nameLength = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).getShort(12).toInt()
            bytes[14 + nameLength + 19] = role.toByte()
            every { assets.open("car.bin") } answers { bytes.inputStream() }
            val groups = CarAssets.load(context)
            assertEquals("version=$version role=$role", role == 7, groups.isNotEmpty())
            if (role == 7) assertEquals(7, groups.single().lampRole)
        }
    }

    private fun tailLampAsset(version: Int): ByteArray {
        val names = listOf("Car_Paint", "LampTail").map { it.toByteArray() }
        val positions = listOf(
            intArrayOf(-1000, 1000, -2000, 1000, 1000, -2000, 0, 1000, -1000),
            intArrayOf(0, 500, -1500, 100, 500, -1500, 0, 600, -1500,
                2000, 500, -1500, 2100, 500, -1500, 2000, 600, -1500),
        )
        val buffer = ByteBuffer.allocate(1024).order(ByteOrder.LITTLE_ENDIAN)
        buffer.put("CARM".toByteArray()).putInt(version).putInt(2)
        names.forEach { buffer.putShort(it.size.toShort()).put(it) }
        positions.forEachIndexed { i, p ->
            val count = p.size / 3
            buffer.putInt(i).putInt(count).putInt(count).putInt(0)
            buffer.put(if (i == 0) 6.toByte() else 0.toByte()).put(0).put(50)
            buffer.put(if (i == 0) 0.toByte() else 7.toByte())
            buffer.putFloat(0f).putFloat(1f).putFloat(-1f).putInt(0)
            p.forEach { buffer.putShort(it.toShort()) }
            repeat(count) { buffer.put(0).put(0).put(127) }
            repeat(count) { buffer.putShort(it.toShort()) }
        }
        return buffer.array().copyOf(buffer.position())
    }

    private fun assertTriangleNormals(indices32: Boolean) {
        for (firstVertex in listOf(0, 32766, 32768, 65533)) {
            // Given: the same counterclockwise XY triangle at different vertex offsets.
            val bytes = triangleAsset(firstVertex, indices32)
            every { assets.open("car.bin") } answers { bytes.inputStream() }

            // When: exercise the real binary decoder and its normal reconstruction.
            val groups = CarAssets.load(context)

            // Then: an XY triangle faces +Z, including indices 32768 through 65535.
            assertEquals(1, groups.size)
            val group = groups.single()
            assertEquals(3, group.indices.size)
            for (corner in 0..2) {
                val vertex = firstVertex + corner
                assertEquals(vertex, group.indices[corner].toInt() and 0xFFFF)
                val label = "vertex=" + vertex + ", indices32=" + indices32
                assertEquals(label, 0f, group.verts[vertex * 6 + 3], 0.00001f)
                assertEquals(label, 0f, group.verts[vertex * 6 + 4], 0.00001f)
                assertEquals(label, 1f, group.verts[vertex * 6 + 5], 0.00001f)
            }
        }
    }

    private fun triangleAsset(
        firstVertex: Int, indices32: Boolean, version: Int = 4, positionScale: Float = 0f, edge: Int = 1000,
    ): ByteArray {
        val name = "Car_ChromePlastic".toByteArray(Charsets.UTF_8)
        val count = firstVertex + 3
        val buffer = ByteBuffer.allocate(12 + 2 + name.size + 36 + count * 9 + 3 * (if (indices32) 4 else 2))
            .order(ByteOrder.LITTLE_ENDIAN)
        buffer.put("CARM".toByteArray(Charsets.US_ASCII))
        buffer.putInt(version).putInt(1)
        buffer.putShort(name.size.toShort()).put(name)
        buffer.putInt(0).putInt(count).putInt(3).putInt(if (indices32) 1 else 0)
        buffer.put(0).put(1).put(0).put(0)
        buffer.putFloat(0f).putFloat(0f).putFloat(0f).putFloat(positionScale)
        repeat(count) { vertex ->
            buffer.putShort(if (vertex == firstVertex + 1) edge.toShort() else 0.toShort())
            buffer.putShort(if (vertex == firstVertex + 2) edge.toShort() else 0.toShort())
            buffer.putShort(0)
        }
        repeat(count) { buffer.put(0).put(0).put(127) }
        repeat(3) { corner ->
            if (indices32) buffer.putInt(firstVertex + corner)
            else buffer.putShort((firstVertex + corner).toShort())
        }
        return buffer.array()
    }
}
