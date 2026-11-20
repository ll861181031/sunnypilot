package com.jixiexiaoge.drivingassist.camera

import android.media.MediaCodec
import android.media.MediaFormat
import android.util.Log
import android.view.Surface
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import java.io.Closeable
import java.nio.ByteBuffer
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

private const val TAG = "H264WsPlayer"

/**
 * 备用视频通道（8.5.0）：车端把 encoderd 已经编好的 H.264 通过一条 WS 直接发过来。
 *
 * 为什么要有它：手机跑在卓易通容器里，UDP 源端口会被容器 NAT 反复改写，
 * WebRTC 的 ICE consent（RFC7675）约 20 秒就判死会话，画面只能重建。
 * H.264 over TCP/WS 不受 NAT 改写影响——7000/7714 这两条 WS 一直很稳就是证据。
 *
 * 帧格式（车端 h264_ws.py 定义）：
 * ```
 * [4B 总长度 BE][4B pts_ms BE][1B flags][Annex-B payload...]
 * flags bit0 = 关键帧
 * ```
 *
 * 解码用 MediaCodec 硬解，直接输出到 [SurfaceView] 的 Surface，几乎不吃 CPU。
 */
internal data class H264Playback(
    val connected: Boolean = false,
    val fps: Int = 0,
    val frames: Long = 0,
    val kbps: Int = 0,
    val keyframes: Long = 0,
    val decodeErrors: Long = 0,
    val reason: String = "",
    /** 这一路视频有没有走 GL 双三次上采样（SR）渲染 */
    val sr: Boolean = false,
    /** SR 没走成时的原因（台架排查用，空表示正常或未启用） */
    val srNote: String = "",
) {
    /** 一行仪表，和 WebRTC 那条保持同样的信息密度 */
    fun diagnostics(): String =
        "H264 $fps fps · $frames 帧 · $kbps kbps · K$keyframes" +
            (if (sr) " · SR" else if (srNote.isNotEmpty()) " · 直出[$srNote]" else " · 直出") +
            if (decodeErrors > 0) " · 错误$decodeErrors" else ""
}

internal class H264WsPlayer : Closeable {
    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // WS 长连接，不设读超时
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    private val mutableState = MutableStateFlow(H264Playback())
    val state = mutableState.asStateFlow()

    private val queue = ArrayBlockingQueue<Frame>(MAX_QUEUE)
    private val running = AtomicBoolean(false)
    private val frames = AtomicLong()
    private val keyframes = AtomicLong()
    private val decodeErrors = AtomicLong()
    private val bytesWindow = AtomicLong()

    private var ws: WebSocket? = null
    private var codec: MediaCodec? = null
    private var decoderThread: Thread? = null
    private var surface: Surface? = null
    /** 非空表示这一路走了 SR（SurfaceTexture + GL 双三次上采样） */
    private var srRenderer: GlSrRenderer? = null
    private var currentHost: String = ""
    private var portIndex = 0
    /** 断线重连：车端 shim 重启、Wi-Fi 抖动都要能自己恢复，不用人点「重新连接」 */
    @Volatile private var explicitStop = false
    private val reconnectPending = AtomicBoolean(false)
    private var reconnectAttempt = 0
    @Volatile private var lastPacketAtMs = 0L
    /** 已经解出过帧就不再换端口（换了反而会打断正在出画面的流） */
    private val gotFrame = AtomicBoolean(false)

    private data class Frame(val data: ByteArray, val keyframe: Boolean, val ptsMs: Long)

    /** 连上并开始往 [surface] 解码；`srEnabled` 时先用 GL 上采样再画上去。 */
    fun start(host: String, target: Surface, srEnabled: Boolean = true) {
        stop()
        if (!running.compareAndSet(false, true)) return
        surface = target
        // SR 渲染器把解码结果接进 SurfaceTexture，再用双三次插值画到 SurfaceView；
        // 任何一步失败都退回「解码直出」，不能因为加特效把画面搞没
        var srNote = ""
        val renderer = if (srEnabled) {
            runCatching { GlSrRenderer(target) }.getOrElse { error ->
                srNote = error.message?.take(32) ?: "init"
                Log.w(TAG, "SR 渲染器初始化失败，回退直出: ${error.message}")
                null
            }
        } else {
            srNote = "关闭"
            null
        }
        // SR 路径失败时必须把它完整拆掉再试直出：
        // GlSrRenderer 一旦 eglCreateWindowSurface 成功，这个 Surface 就被 EGL buffer queue 占住，
        // MediaCodec 再 configure 同一个 Surface 会失败。
        var ok = if (renderer != null) {
            runCatching { prepareDecoder(renderer.inputSurface) }.getOrElse { error ->
                Log.w(TAG, "SR 通道解码器失败: ${error.message}")
                false
            }
        } else {
            false
        }
        if (!ok && renderer != null) {
            srNote = "decoder"
            runCatching { renderer.release() }
            Log.w(TAG, "SR 不可用，改用解码直出")
        }
        if (renderer != null && ok) {
            srRenderer = renderer
        }
        if (!ok) {
            ok = runCatching { prepareDecoder(target) }.getOrElse { error ->
                Log.w(TAG, "H.264 解码器初始化失败", error)
                running.set(false)
                false
            }
        }
        if (!ok) {
            mutableState.value = H264Playback(connected = false, reason = "解码器不可用")
            return
        }

        decoderThread = Thread({ decodeLoop() }, "h264-decode").apply { isDaemon = true; start() }

        currentHost = host
        gotFrame.set(false)
        portIndex = 0
        explicitStop = false
        reconnectAttempt = 0
        mutableState.value = H264Playback(srNote = srNote)
        connect()
        }

    /** 连下一个候选端口（7000 是 shim 上挂的正式路径，7708 是独立服务的旧端口） */
    private fun connect() {
        val url = "ws://$currentHost:${PORT_CANDIDATES[portIndex]}/ws/camera/road"
        Log.i(TAG, "连接 H.264 通道 $url")
        ws = client.newWebSocket(
            Request.Builder().url(url).build(),
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    Log.i(TAG, "备用视频通道已连接")
                    mutableState.value = mutableState.value.copy(connected = true, reason = "")
                }

                override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                    onFramePacket(bytes.toByteArray())
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    Log.w(TAG, "H.264 通道断开: ${t.message}")
                    mutableState.value = mutableState.value.copy(
                        connected = false,
                        fps = 0,
                        reason = t.message ?: t.javaClass.simpleName,
                    )
                    failover()
                    scheduleReconnect()
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    mutableState.value = mutableState.value.copy(connected = false, fps = 0, reason = reason)
                    failover()
                    scheduleReconnect()
                }
            },
        )
    }

    private fun failover() {
        if (gotFrame.get() || !running.get()) return
        if (portIndex + 1 >= PORT_CANDIDATES.size) return
        portIndex++
        Log.i(TAG, "换端口重连: ${PORT_CANDIDATES[portIndex]}")
        connect()
    }

    /** 断线重连（1s → 2s → 4s → 8s，封顶 10s）；只在播放器还在跑时生效 */
    private fun scheduleReconnect() {
        if (explicitStop || !running.get()) return
        if (!reconnectPending.compareAndSet(false, true)) return
        val delayMs = minOf(RECONNECT_BASE_MS shl reconnectAttempt.coerceAtMost(4), RECONNECT_MAX_MS)
        reconnectAttempt++
        Log.i(TAG, "H.264 通道 ${delayMs}ms 后重连（第 $reconnectAttempt 次）")
        Thread {
            try {
                Thread.sleep(delayMs)
            } catch (interrupted: InterruptedException) {
                return@Thread
            }
            reconnectPending.set(false)
            if (!explicitStop && running.get()) connect()
        }.apply { isDaemon = true }.start()
    }

    private fun onFramePacket(packet: ByteArray) {
        if (packet.size < HEADER_SIZE) return
        val buffer = ByteBuffer.wrap(packet)
        val total = buffer.int
        val ptsMs = buffer.int.toLong() and 0xFFFFFFFFL
        val flags = buffer.get().toInt() and 0xFF
        if (total != packet.size) return
        val payload = ByteArray(packet.size - HEADER_SIZE)
        buffer.get(payload)
        val keyframe = flags and 1 == 1
        if (keyframe) keyframes.incrementAndGet()
        gotFrame.set(true)
        lastPacketAtMs = System.nanoTime() / 1_000_000
        reconnectAttempt = 0
        bytesWindow.addAndGet(packet.size.toLong())
        // 队列满就丢最旧的，宁可少几帧也不让延迟滚雪球
        if (!queue.offer(Frame(payload, keyframe, ptsMs))) {
            queue.poll()
            queue.offer(Frame(payload, keyframe, ptsMs))
        }
    }

    private fun prepareDecoder(target: Surface): Boolean {
        val mediaCodec = MediaCodec.createDecoderByType(MIME)
        val format = MediaFormat.createVideoFormat(MIME, FALLBACK_WIDTH, FALLBACK_HEIGHT).apply {
            // 车端每帧都带 SPS/PPS（全 I 帧），配置里不强制 CSD；
            // 首帧再用真实 SPS/PPS 补 csd-0/csd-1，兼容某些严格要求 CSD 的解码器。
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, MAX_INPUT)
        }
        mediaCodec.configure(format, target, null, 0)
        mediaCodec.start()
        codec = mediaCodec
        return true
    }

    private fun decodeLoop() {
        val mediaCodec = codec ?: return
        val info = MediaCodec.BufferInfo()
        var fpsWindowStart = System.nanoTime()
        var fpsFrames = 0

        while (running.get()) {
            val frame = try {
                queue.poll(200, TimeUnit.MILLISECONDS)
            } catch (interrupted: InterruptedException) {
                break
            }
            if (frame != null) {
                try {
                    val inputIndex = mediaCodec.dequeueInputBuffer(INPUT_TIMEOUT_US)
                    if (inputIndex >= 0) {
                        val input = mediaCodec.getInputBuffer(inputIndex) ?: continue
                        input.clear()
                        input.put(frame.data)
                        val flags = if (frame.keyframe) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0
                        mediaCodec.queueInputBuffer(
                            inputIndex, 0, frame.data.size,
                            frame.ptsMs * 1000L, flags,
                        )
                    }
                } catch (error: Throwable) {
                    decodeErrors.incrementAndGet()
                    Log.w(TAG, "喂帧失败: ${error.message}")
                    // 解码器状态可能已经坏了，重建一次
                    runCatching { mediaCodec.stop(); mediaCodec.start() }
                }
            }
            // 排空输出
            while (true) {
                val outIndex = try {
                    mediaCodec.dequeueOutputBuffer(info, 0)
                } catch (error: Throwable) {
                    Log.w(TAG, "取输出失败: ${error.message}")
                    break
                }
                when {
                    outIndex >= 0 -> {
                        mediaCodec.releaseOutputBuffer(outIndex, true)
                        frames.incrementAndGet()
                        fpsFrames++
                    }
                    outIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                        val changed = mediaCodec.outputFormat
                        val w = changed.getInteger(MediaFormat.KEY_WIDTH)
                        val h = changed.getInteger(MediaFormat.KEY_HEIGHT)
                        Log.i(TAG, "H.264 输出格式: ${w}x${h}")
                        srRenderer?.updateSourceSize(w, h)
                        if (srRenderer != null) {
                            mutableState.value = mutableState.value.copy(sr = true)
                        }
                    }
                    else -> break
                }
            }
            val nowMs = System.nanoTime() / 1_000_000
            if (mutableState.value.connected && lastPacketAtMs > 0 &&
                nowMs - lastPacketAtMs > STALL_RECONNECT_MS
            ) {
                Log.w(TAG, "H.264 通道 6 秒没收到帧，主动重连")
                lastPacketAtMs = nowMs
                runCatching { ws?.cancel() }
            }
            val now = System.nanoTime()
            if (now - fpsWindowStart >= 1_000_000_000L) {
                val fps = fpsFrames
                fpsFrames = 0
                fpsWindowStart = now
                val kbps = (bytesWindow.getAndSet(0L) * 8 / 1000).toInt()
                mutableState.value = mutableState.value.copy(
                    fps = fps,
                    frames = frames.get(),
                    keyframes = keyframes.get(),
                    decodeErrors = decodeErrors.get(),
                    kbps = kbps,
                )
            }
        }
    }

    fun stop() {
        explicitStop = true
        if (!running.compareAndSet(true, false)) {
            closeQuietly()
            return
        }
        ws?.close(1000, "client stop")
        ws = null
        decoderThread?.join(400)
        decoderThread = null
        closeQuietly()
        mutableState.value = H264Playback()
    }

    private fun closeQuietly() {
        queue.clear()
        runCatching { codec?.stop() }
        runCatching { codec?.release() }
        codec = null
        runCatching { srRenderer?.release() }
        srRenderer = null
        surface = null
    }

    override fun close() = stop()

    companion object {
        /**
         * 只走 shim 的 7000（`/ws/camera/road`）——不再额外占端口。
         * 8.5.6 曾试过"7000 失败自动退到 7708"，但那样失败原因会被悄悄掩盖，
         * 而且两条通道协议完全一样，没有回退的必要。
         */
        val PORT_CANDIDATES = listOf(7000)
        private const val MIME = "video/avc"
        private const val HEADER_SIZE = 9
        private const val MAX_QUEUE = 24
        private const val MAX_INPUT = 2 * 1024 * 1024
        private const val INPUT_TIMEOUT_US = 10_000L
        private const val RECONNECT_BASE_MS = 1_000L
        private const val RECONNECT_MAX_MS = 10_000L
        /** 超过这么久没收到帧就主动重连（正常 20fps，50ms 一帧） */
        private const val STALL_RECONNECT_MS = 6_000L
        private const val FALLBACK_WIDTH = 1344
        private const val FALLBACK_HEIGHT = 760
    }
}
