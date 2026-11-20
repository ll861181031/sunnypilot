package com.jixiexiaoge.drivingassist.camera

import android.graphics.SurfaceTexture
import android.opengl.EGL14
import android.opengl.EGLConfig
import android.opengl.EGLContext
import android.opengl.EGLDisplay
import android.opengl.EGLSurface
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Surface
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 视频超分（SR）渲染器。
 *
 * 车端送上来的 H.264 是 964×604 / 1928×1208，原来 MediaCodec 直接画到 SurfaceView，
 * 放大靠 GPU 默认的双线性（每像素 4 个采样点），放大 2 倍以上边缘会糊。这里把解码目标
 * 换成 [SurfaceTexture]，在 GL 里用 **Catmull-Rom 双三次插值**（16 tap）重新采样，
 * 再画到 SurfaceView 上。
 *
 * ```
 * MediaCodec → SurfaceTexture(OES) → [本类] 双三次上采样 → SurfaceView
 * ```
 *
 * 三个硬性约束（都是踩过的坑）：
 *  1. **先编译着色器，最后才碰 SurfaceView 的 Surface**。着色器在无窗口 surface 的
 *     context 下就能编译；一旦 `eglCreateWindowSurface` 成功，这个 Surface 就被 EGL
 *     的 buffer queue 占住，MediaCodec 再 configure 同一个 Surface 会失败 —— 那会让
 *     「SR 失败自动回退直出」这条兜底路径也一起失效，视频直接黑屏。
 *  2. **用 ES 2.0 + GL_OES_EGL_image_external**（不是 ES3 的 `_essl3` 变体）。
 *     OES 外部纹理在 ES2 下是 Android 全平台支持最广的组合。
 *  3. **失败要把 EGL 全部拆干净**再抛异常，不能留挂着的 surface/context。
 *
 * 线程模型：所有 GL 调用都在内部 `gl-sr` 线程；外部只用 [createInputSurface] /
 * [notifyFrameAvailable] / [updateSourceSize] / [release]。
 */
internal class GlSrRenderer(
    private val outputSurface: Surface,
    private val tagPrefix: String = "GlSr",
) {
    private val TAG = tagPrefix

    /** 给 MediaCodec 用的输入 Surface；解码结果进 SurfaceTexture */
    val inputSurface: Surface

    private val thread = HandlerThread("gl-sr").apply { start() }
    private val handler = Handler(thread.looper)
    private val released = AtomicBoolean(false)

    // ── 以下全部只在 GL 线程访问 ──────────────────────────────────────────
    private var eglDisplay: EGLDisplay = EGL14.EGL_NO_DISPLAY
    private var eglContext: EGLContext = EGL14.EGL_NO_CONTEXT
    private var eglSurface: EGLSurface = EGL14.EGL_NO_SURFACE
    private var program = 0
    private var textureId = 0
    private var surfaceTexture: SurfaceTexture? = null
    private var aPosLoc = -1
    private var uTexMatrixLoc = -1
    private var uTexSizeLoc = -1

    private val vertexBuffer: FloatBuffer = ByteBuffer
        .allocateDirect(4 * 2 * 4)
        .order(ByteOrder.nativeOrder())
        .asFloatBuffer()
        .apply {
            put(floatArrayOf(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f))
            position(0)
        }

    private val texMatrix = FloatArray(16)
    @Volatile private var failReason = ""
    private val framePending = AtomicBoolean(false)

    @Volatile private var srcWidth = 1344
    @Volatile private var srcHeight = 760

    init {
        val latch = CountDownLatch(1)
        val ok = AtomicBoolean(false)
        handler.post {
            try {
                val failure = setupGl()
                if (failure == null) {
                    val st = SurfaceTexture(textureId)
                    st.setOnFrameAvailableListener { scheduleDraw() }
                    surfaceTexture = st
                    ok.set(true)
                } else {
                    failReason = failure
                    Log.w(TAG, "GL 初始化失败: $failure")
                    teardownGl()
                }
            } catch (error: Throwable) {
                failReason = error.message?.take(32) ?: "exception"
                Log.w(TAG, "GL 初始化异常: ${error.message}")
                teardownGl()
            } finally {
                latch.countDown()
            }
        }
        // EGL 正常 30~80ms 建好；600ms 建不出来就放弃，让调用方回退直出
        if (!latch.await(600, TimeUnit.MILLISECONDS) || !ok.get()) {
            handler.post { teardownGl() }
            runCatching { thread.quitSafely() }
            if (failReason.isEmpty()) failReason = "timeout"
            throw IllegalStateException(failReason)
        }
        inputSurface = Surface(surfaceTexture!!)
    }

    /** 解码器换了分辨率时调用（来自 MediaCodec 的 INFO_OUTPUT_FORMAT_CHANGED） */
    fun updateSourceSize(width: Int, height: Int) {
        if (width <= 0 || height <= 0) return
        if (width == srcWidth && height == srcHeight) return
        srcWidth = width
        srcHeight = height
    }

    fun notifyFrameAvailable() = scheduleDraw()

    private fun scheduleDraw() {
        if (released.get() || !framePending.compareAndSet(false, true)) return
        handler.post {
            framePending.set(false)
            if (!released.get()) drawFrame()
        }
    }

    fun release() {
        if (!released.compareAndSet(false, true)) return
        val latch = CountDownLatch(1)
        handler.post {
            surfaceTexture?.setOnFrameAvailableListener(null)
            runCatching { surfaceTexture?.release() }
            surfaceTexture = null
            runCatching { inputSurface.release() }
            teardownGl()
            latch.countDown()
        }
        latch.await(800, TimeUnit.MILLISECONDS)
        runCatching { thread.quitSafely() }
    }

    // ── GL 实现 ────────────────────────────────────────────────────────────

    /**
     * 顺序很关键：**先建 context 并编译着色器（此时不碰 SurfaceView），
     * 全部成功后才 eglCreateWindowSurface**。着色器失败时 SurfaceView 的 Surface
     * 完全没被触碰，调用方回退「解码直出」仍然可用。
     */
    /** null 表示成功；否则是给 HUD 看的简短失败原因 */
    private fun setupGl(): String? {
        eglDisplay = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY)
        if (eglDisplay == EGL14.EGL_NO_DISPLAY) {
            return "no-display"
        }
        val version = IntArray(2)
        if (!EGL14.eglInitialize(eglDisplay, version, 0, version, 1)) {
            val e = EGL14.eglGetError()
            eglDisplay = EGL14.EGL_NO_DISPLAY
            return "eglInit:$e"
        }
        val configs = arrayOfNulls<EGLConfig>(1)
        val numConfigs = IntArray(1)
        val attribs = intArrayOf(
            EGL14.EGL_RED_SIZE, 8,
            EGL14.EGL_GREEN_SIZE, 8,
            EGL14.EGL_BLUE_SIZE, 8,
            EGL14.EGL_ALPHA_SIZE, 8,
            EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
            EGL14.EGL_SURFACE_TYPE, EGL14.EGL_WINDOW_BIT,
            EGL14.EGL_NONE,
        )
        if (!EGL14.eglChooseConfig(eglDisplay, attribs, 0, configs, 0, 1, numConfigs, 0) ||
            numConfigs[0] == 0
        ) {
            return "no-config"
        }
        val config = configs[0] ?: return "no-config"
        eglContext = EGL14.eglCreateContext(
            eglDisplay, config, EGL14.EGL_NO_CONTEXT,
            intArrayOf(EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE), 0,
        )
        if (eglContext == EGL14.EGL_NO_CONTEXT) {
            return "ctx:${EGL14.eglGetError()}"
        }
        // 无窗口 surface 也能编译着色器 —— 这一步失败不该影响视频直出
        if (!EGL14.eglMakeCurrent(
                eglDisplay, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, eglContext,
            )
        ) {
            return "mkcur:${EGL14.eglGetError()}"
        }
        program = buildProgram()
        if (program == 0) return "shader"

        aPosLoc = GLES20.glGetAttribLocation(program, "aPos")
        uTexMatrixLoc = GLES20.glGetUniformLocation(program, "uTexMatrix")
        uTexSizeLoc = GLES20.glGetUniformLocation(program, "uTexSize")

        val tex = IntArray(1)
        GLES20.glGenTextures(1, tex, 0)
        textureId = tex[0]
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, textureId)
        GLES20.glTexParameterf(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR.toFloat(),
        )
        GLES20.glTexParameterf(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR.toFloat(),
        )
        GLES20.glTexParameteri(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE,
        )
        GLES20.glTexParameteri(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE,
        )

        // 着色器都就绪了，这才接管 SurfaceView 的 Surface
        eglSurface = EGL14.eglCreateWindowSurface(
            eglDisplay, config, outputSurface, intArrayOf(EGL14.EGL_NONE), 0,
        )
        if (eglSurface == EGL14.EGL_NO_SURFACE) {
            return "window:${EGL14.eglGetError()}"
        }
        if (!EGL14.eglMakeCurrent(eglDisplay, eglSurface, eglSurface, eglContext)) {
            return "mkcur2:${EGL14.eglGetError()}"
        }
        return null
    }

    private fun teardownGl() {
        runCatching {
            if (program != 0) GLES20.glDeleteProgram(program)
            program = 0
            if (textureId != 0) GLES20.glDeleteTextures(1, intArrayOf(textureId), 0)
            textureId = 0
            if (eglDisplay != EGL14.EGL_NO_DISPLAY) {
                EGL14.eglMakeCurrent(
                    eglDisplay, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT,
                )
                if (eglSurface != EGL14.EGL_NO_SURFACE) {
                    EGL14.eglDestroySurface(eglDisplay, eglSurface)
                }
                if (eglContext != EGL14.EGL_NO_CONTEXT) {
                    EGL14.eglDestroyContext(eglDisplay, eglContext)
                }
                EGL14.eglTerminate(eglDisplay)
            }
        }.onFailure { Log.w(TAG, "teardown: ${it.message}") }
        eglSurface = EGL14.EGL_NO_SURFACE
        eglContext = EGL14.EGL_NO_CONTEXT
        eglDisplay = EGL14.EGL_NO_DISPLAY
    }

    private fun drawFrame() {
        val st = surfaceTexture ?: return
        if (eglDisplay == EGL14.EGL_NO_DISPLAY || eglSurface == EGL14.EGL_NO_SURFACE) return
        try {
            st.updateTexImage()
            st.getTransformMatrix(texMatrix)
        } catch (error: Throwable) {
            Log.w(TAG, "updateTexImage: ${error.message}")
            return
        }
        val size = IntArray(2)
        EGL14.eglQuerySurface(eglDisplay, eglSurface, EGL14.EGL_WIDTH, size, 0)
        EGL14.eglQuerySurface(eglDisplay, eglSurface, EGL14.EGL_HEIGHT, size, 1)
        val viewWidth = size[0]
        val viewHeight = size[1]
        if (viewWidth <= 0 || viewHeight <= 0) return

        // 等比 + letterbox：不这么做会把 964×604 硬拉成屏幕比例，画面变形
        val srcAspect = srcWidth.toFloat() / srcHeight.toFloat()
        val viewAspect = viewWidth.toFloat() / viewHeight.toFloat()
        var vpW = viewWidth
        var vpH = viewHeight
        if (srcAspect > viewAspect) {
            vpH = (viewWidth / srcAspect).toInt()
        } else {
            vpW = (viewHeight * srcAspect).toInt()
        }
        if (vpW <= 0 || vpH <= 0) return

        GLES20.glViewport(0, 0, viewWidth, viewHeight)
        GLES20.glClearColor(0f, 0f, 0f, 1f)
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)
        GLES20.glViewport((viewWidth - vpW) / 2, (viewHeight - vpH) / 2, vpW, vpH)

        GLES20.glUseProgram(program)
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, textureId)
        GLES20.glUniform1i(GLES20.glGetUniformLocation(program, "uTex"), 0)
        GLES20.glUniformMatrix4fv(uTexMatrixLoc, 1, false, texMatrix, 0)
        GLES20.glUniform2f(uTexSizeLoc, srcWidth.toFloat(), srcHeight.toFloat())

        GLES20.glEnableVertexAttribArray(aPosLoc)
        GLES20.glVertexAttribPointer(aPosLoc, 2, GLES20.GL_FLOAT, false, 0, vertexBuffer)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)
        GLES20.glDisableVertexAttribArray(aPosLoc)

        if (!EGL14.eglSwapBuffers(eglDisplay, eglSurface)) {
            Log.w(TAG, "eglSwapBuffers 失败 err=${EGL14.eglGetError()}")
        }
    }

    private fun buildProgram(): Int {
        val vs = compileShader(GLES20.GL_VERTEX_SHADER, VERTEX_SHADER)
        if (vs == 0) return 0
        val fs = compileShader(GLES20.GL_FRAGMENT_SHADER, FRAGMENT_SHADER)
        if (fs == 0) {
            GLES20.glDeleteShader(vs)
            return 0
        }
        val p = GLES20.glCreateProgram()
        GLES20.glAttachShader(p, vs)
        GLES20.glAttachShader(p, fs)
        GLES20.glLinkProgram(p)
        val status = IntArray(1)
        GLES20.glGetProgramiv(p, GLES20.GL_LINK_STATUS, status, 0)
        GLES20.glDeleteShader(vs)
        GLES20.glDeleteShader(fs)
        if (status[0] == 0) {
            Log.w(TAG, "链接着色器失败: ${GLES20.glGetProgramInfoLog(p)}")
            GLES20.glDeleteProgram(p)
            return 0
        }
        return p
    }

    private fun compileShader(type: Int, source: String): Int {
        val shader = GLES20.glCreateShader(type)
        if (shader == 0) return 0
        GLES20.glShaderSource(shader, source)
        GLES20.glCompileShader(shader)
        val status = IntArray(1)
        GLES20.glGetShaderiv(shader, GLES20.GL_COMPILE_STATUS, status, 0)
        if (status[0] == 0) {
            Log.w(TAG, "编译着色器失败: ${GLES20.glGetShaderInfoLog(shader)}")
            GLES20.glDeleteShader(shader)
            return 0
        }
        return shader
    }

    companion object {
        /** ES 2.0：兼容面最广，OES 外部纹理在 ES2 下所有 Android 设备都支持 */
        private const val VERTEX_SHADER = """
            attribute vec2 aPos;
            uniform mat4 uTexMatrix;
            varying vec2 vUv;
            void main() {
                gl_Position = vec4(aPos, 0.0, 1.0);
                vUv = (uTexMatrix * vec4(aPos * 0.5 + 0.5, 0.0, 1.0)).xy;
            }
        """

        /**
         * Catmull-Rom 双三次上采样，16 tap（4×4 邻域张量积）。
         *
         * 权重核：
         *   w(t) = 1.5|t|³ - 2.5|t|² + 1          (|t| ≤ 1)
         *   w(t) = -0.5|t|³ + 2.5|t|² - 4|t| + 2  (1 < |t| < 2)
         *
         * 相比 GL 默认双线性的 4 个采样点，双三次同时看周围 16 个像素，
         * 放大后边缘更利、细节不容易糊成一片。
         */
        private const val FRAGMENT_SHADER = """
            #extension GL_OES_EGL_image_external : require
            precision highp float;
            varying vec2 vUv;
            uniform samplerExternalOES uTex;
            uniform vec2 uTexSize;

            float cubicWeight(float t) {
                float a = abs(t);
                if (a <= 1.0) {
                    return (1.5 * a - 2.5) * a * a + 1.0;
                }
                if (a < 2.0) {
                    return ((-0.5 * a + 2.5) * a - 4.0) * a + 2.0;
                }
                return 0.0;
            }

            void main() {
                vec2 pos = vUv * uTexSize - 0.5;
                vec2 cell = floor(pos);
                vec2 f = pos - cell;

                float wx0 = cubicWeight(f.x + 1.0);
                float wx1 = cubicWeight(f.x);
                float wx2 = cubicWeight(1.0 - f.x);
                float wx3 = cubicWeight(2.0 - f.x);
                float wy0 = cubicWeight(f.y + 1.0);
                float wy1 = cubicWeight(f.y);
                float wy2 = cubicWeight(1.0 - f.y);
                float wy3 = cubicWeight(2.0 - f.y);

                vec2 inv = 1.0 / uTexSize;
                vec3 acc = vec3(0.0);
                float wsum = 0.0;

                for (int oy = -1; oy <= 2; oy++) {
                    float wy = oy == -1 ? wy0 : (oy == 0 ? wy1 : (oy == 1 ? wy2 : wy3));
                    for (int ox = -1; ox <= 2; ox++) {
                        float wx = ox == -1 ? wx0 : (ox == 0 ? wx1 : (ox == 1 ? wx2 : wx3));
                        vec2 tc = (cell + vec2(float(ox), float(oy)) + 0.5) * inv;
                        float w = wx * wy;
                        acc += texture2D(uTex, clamp(tc, vec2(0.0), vec2(1.0))).rgb * w;
                        wsum += w;
                    }
                }
                gl_FragColor = vec4(acc / max(wsum, 0.0001), 1.0);
            }
        """
    }
}